"""Quality 2.0 (deepen.py): the follow-up builder, the audit, the improve guards, the loop and its shared cache prefix
(Claude and Jev faked)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from explainer import data, deepen, pipeline
from explainer.core.followup import CHANGE_RULES
from explainer.core.validate import assemble_spec
from explainer.js import json_parse
from explainer.llm import cached_system
from explainer.pipeline import planner_system
from explainer.pricing import UsageLog
from explainer.requests import parse_generate
from explainer.revise import improve_request, page_block

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))
OUTLINE, DESIGN = SAMPLE["outline"], SAMPLE["design"]


def base_spec(n_stages: int | None = None, **extra) -> dict:
    outline = {**OUTLINE, "stages": OUTLINE["stages"][:n_stages]} if n_stages else OUTLINE
    spec = assemble_spec(outline, DESIGN, "Outline\n\nSource material:\nReader's request: How do transformers work?", [])
    return {**spec, "design": {**spec["design"], "textSize": "large"}, **extra}


def ids(spec: dict) -> list[str]:
    return [s["id"] for s in spec["stages"]]


# ---------- the follow-up ----------


def test_follow_up_of_is_a_revise_request():
    spec = base_spec()
    f = deepen.follow_up_of(spec)
    assert f["kind"] == "revise" and "add" not in f
    assert f["source"] == "Reader's request: How do transformers work?"
    outline = json_parse(f["outline"])
    assert [s["id"] for s in outline["stages"]] == ids(spec)
    assert all("icon" not in s for s in outline["stages"]) and "chatbot" not in outline
    assert len(f["design"]["stageIcons"]) == len(spec["stages"])


def test_compact_outline_drops_example_steps_and_widgets():
    spec = base_spec()
    spec["stages"][0]["blocks"] = [{"type": "examples", "items": [{"title": "t", "steps": ["a"]}]}, {"type": "widget", "title": "Toy"}]
    assert deepen.compact_outline(spec)["stages"][0]["blocks"] == [{"type": "examples", "items": [{"title": "t"}]}, {"type": "text", "md": "(interactive widget: Toy)"}]
    assert deepen.kept_of(spec) == {"steps": {f"{spec['stages'][0]['id']}:0:0": ["a"]}}


def test_follow_up_of_refuses_a_page_too_large():
    spec = base_spec()
    spec["stages"][0]["blocks"] = [{"type": "text", "md": "x" * 100_000}]
    assert isinstance(deepen.follow_up_of(spec), str)


# ---------- the audit and its cache prefix ----------


class FakeClient:
    def __init__(self, reply: str):
        self.reply, self.kwargs = reply, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.kwargs = kw
        return SimpleNamespace(model="claude-haiku-5-5", stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.reply)],
                               usage={"input_tokens": 10, "output_tokens": 5})


FINDINGS = [{"id": "f1", "check": "facts", "stage": "tokens", "detail": "wrong"}, {"id": "f2", "check": "style", "stage": "tokens", "detail": "wordy"},
            {"id": "f3", "check": "coverage", "stage": "", "detail": "Missing topic"}]


def test_audit_cleans_items_and_shares_the_improve_prefix(monkeypatch):
    spec = base_spec()
    sid = spec["stages"][1]["id"]
    reply = json.dumps({"items": [
        {"findings": ["f1"], "verdict": "real", "action": "modify", "stage": sid, "after": "", "instruction": "Fix the number."},
        {"findings": ["f2", "f9"], "verdict": "false_alarm", "action": "none", "stage": sid, "after": "", "instruction": ""},
        {"findings": ["f3"], "verdict": "real", "action": "add_section", "stage": "", "after": sid, "instruction": "Add positional encoding."},
        {"findings": [], "verdict": "real", "action": "modify", "stage": "no-such-stage", "after": "", "instruction": "x"},
    ]})
    client = FakeClient("Here is the audit:\n" + reply)
    f = deepen.follow_up_of(spec)
    items = deepen.audit(client, f, spec, FINDINGS, "low", UsageLog("quality-deep"))
    assert [(i["verdict"], i["action"]) for i in items] == [("real", "modify"), ("false_alarm", "none"), ("real", "add_section"), ("false_alarm", "none")]
    assert items[1]["findings"] == ["f2"] and items[2]["after"] == sid
    # Same tools, tool_choice, system and first user block as the improve plan, so it reads the page from cache.
    kw = client.kwargs
    assert kw["tools"] == [data.formats()["computeTool"]] and kw["tool_choice"] == {"type": "auto"}
    assert kw["system"] == cached_system(planner_system()) and kw["output_config"] == {"effort": "low"}
    improve = improve_request({**f, "improve": "fix it"}, CHANGE_RULES)
    assert kw["messages"][0]["content"][0] == improve[0] == page_block(f)
    assert "cache_control" in page_block(f)


def test_a_later_audit_fixes_errors_only():
    spec = base_spec()
    sid = spec["stages"][1]["id"]
    reply = json.dumps({"items": [
        {"findings": ["f1"], "verdict": "real", "action": "modify", "stage": sid, "after": "", "instruction": "Use 0.25, not 0.5."},
        {"findings": ["f3"], "verdict": "real", "action": "add_section", "stage": "", "after": sid, "instruction": "Add a topic."},
        {"findings": [], "verdict": "real", "action": "adjust_graph", "stage": sid, "after": "", "instruction": "Self-found: fix the axis."},
    ]})
    client = FakeClient(reply)
    items = deepen.audit(client, deepen.follow_up_of(spec), spec, FINDINGS, "low", UsageLog("quality-deep"), later=True)
    assert [(i["verdict"], i["action"]) for i in items] == [("real", "modify"), ("false_alarm", "none"), ("real", "adjust_graph")]
    assert data.prompt("AUDIT_LATER") in client.kwargs["messages"][0]["content"][1]["text"]
    deepen.audit(client, deepen.follow_up_of(spec), spec, FINDINGS, "low", UsageLog("quality-deep"))
    assert data.prompt("AUDIT_LATER") not in client.kwargs["messages"][0]["content"][1]["text"]


def test_the_improve_plan_gets_the_cached_page_first(monkeypatch):
    spec = base_spec(4)
    f = {**deepen.follow_up_of(spec), "improve": "Audit items to apply:\n1. [modify] ..."}
    seen = {}

    def fake_write_outline(client, request, emit, usage, system, parse, effort, fmt="json", budget=None, whole=""):
        seen["request"], seen["system"] = request, system
        old = json_parse(f["outline"])
        return {"outline": old, "warnings": [], "revision": {"changed": [old["stages"][0]["id"]], "removed": 0}}

    monkeypatch.setattr(pipeline, "write_outline", fake_write_outline)
    monkeypatch.setattr(pipeline, "write_examples", lambda client, spec, usage: spec)
    called = []
    monkeypatch.setattr(pipeline, "clarity_check", lambda *a, **k: called.append("clarity"))
    req = {"mode": "concept", "text": "Apply the audit", "quality": "quality-plus", "followUp": f}
    pipeline.generate(None, req, lambda e: None, jev_key="jev", checks=False)
    assert seen["request"][0] == page_block(f) and "Audit items to apply" in seen["request"][1]["text"]
    assert seen["system"] == planner_system() and called == []


# ---------- improve guards ----------


def changed(spec: dict, i: int, md: str = "Rewritten.") -> dict:
    st = spec["stages"][i]
    return {**st, "blocks": [{"type": "text", "md": md}, *st["blocks"][1:]]}


def revised(old: dict, stages: list[dict], changed_ids: list[str], removed: int = 0) -> dict:
    # The shape pipeline.generate really returns: counts only (revision_summary), no list of changed ids.
    old_ids = {s["id"] for s in old["stages"]}
    added = sum(1 for i in changed_ids if i not in old_ids)
    counts = {"added": added, "replaced": len(changed_ids) - added, "removed": removed, "kept": len(stages) - len(changed_ids)}
    return {**old, "stages": stages, "revision": counts, "warnings": []}


def test_guard_finds_changes_without_a_list_of_changed_ids():
    # Regression: guard read revision["changed"], which the real pipeline never sets, so every round
    # looked like "changed nothing" and the loop stopped with the improved stages unverified.
    old = base_spec(4)
    a = old["stages"][0]["id"]
    new_stage = {**old["stages"][0], "id": "added", "label": "Added"}
    new = revised(old, [changed(old, 0), *old["stages"][1:], new_stage], [a, "added"])
    _, idx, notes = deepen.guard(old, new, [item(a), item("", "add_section")])
    assert idx == [0, 4] and notes == []


def item(stage: str, action: str = "modify") -> dict:
    return {"findings": ["f1"], "verdict": "real", "action": action, "stage": stage, "after": "", "instruction": "do it"}


def test_guard_puts_back_stages_the_audit_did_not_name():
    old = base_spec(4)
    a, b = old["stages"][0]["id"], old["stages"][1]["id"]
    new = revised(old, [changed(old, 0), changed(old, 1), *old["stages"][2:]], [a, b])
    kept, idx, notes = deepen.guard(old, new, [item(a)])
    assert kept["stages"][0]["blocks"][0]["md"] == "Rewritten." and kept["stages"][1] == old["stages"][1]
    assert idx == [0] and "put back" in notes[0]


def test_guard_refuses_a_rewrite_or_an_unasked_removal():
    old = base_spec(4)
    a = old["stages"][0]["id"]
    assert deepen.guard(old, {**old, "warnings": []}, [item(a)])[0] is None  # no "revision": a full rewrite
    assert deepen.guard(old, revised(old, old["stages"][1:], [], removed=1), [item(a)])[0] is None
    ok, idx, _ = deepen.guard(old, revised(old, old["stages"][1:], [], removed=1), [item(a, "remove_section")])
    assert ok is not None and ids(ok) == ids(old)[1:] and idx == []


def test_guard_caps_new_stages_per_round():
    old = base_spec(4)
    new_stages = [{**old["stages"][0], "id": f"new{i}", "label": f"New {i}"} for i in range(3)]
    kept, idx, notes = deepen.guard(old, revised(old, [*old["stages"], *new_stages], ["new0", "new1", "new2"]), [item("", "add_section")])
    assert ids(kept)[-2:] == ["new0", "new1"] and len(kept["stages"]) == 6 and idx == [4, 5]
    assert "not added" in notes[0]


# ---------- the loop ----------


class Loop:
    """Fakes for generate_deep: generate (first page and each improve), verify, audit and the finishing checks."""

    def __init__(self, monkeypatch, base: dict, rounds: list[tuple[list, list]]):
        self.rounds = list(rounds)  # per round: (findings, audit items)
        self.calls: list[dict] = []
        self.verified: list = []
        self.errors_only: list[bool] = []
        self.audited: list = []
        self.finish: list = []
        self.notes = 0

        def fake_generate(client, req, emit, jev_key=None, plan_effort="medium", paper_cache=None, usage=None, checks=True):
            assert checks is False
            self.calls.append(req)
            if "followUp" not in req:
                return {"spec": base, "usage": usage.summary()}
            old = json_parse(req["followUp"]["outline"])
            target = next(s for s in old["stages"] if f'stage "{s["id"]}"' in req["followUp"]["improve"])
            stages = [{**s, "icon": base["stages"][0]["icon"], "blocks": [{"type": "text", "md": f"fixed {len(self.calls)}"}, *s["blocks"][1:]]}
                      if s["id"] == target["id"] else next(x for x in self.current["stages"] if x["id"] == s["id"]) for s in old["stages"]]
            spec = {**self.current, "stages": stages, "revision": {"added": 0, "replaced": 1, "removed": 0, "kept": len(stages) - 1}, "design": {**base["design"], "textSize": None},
                    "warnings": [f"improve {len(self.calls)}"]}
            spec.pop("fromMemory", None)
            return {"spec": spec, "usage": usage.summary()}

        def fake_verify(client, jev_key, spec, asked, usage, only=None, errors_only=False):
            self.current = spec
            self.verified.append(only)
            self.errors_only.append(errors_only)
            findings = self.rounds[0][0] if self.rounds else []
            return {"findings": findings, "skipped": [], "stages": 0}

        def fake_audit(client, f, spec, findings, effort, usage, later=False):
            assert json_parse(f["outline"])["stages"][0]["id"] == spec["stages"][0]["id"]
            self.audited.append(findings)
            return self.rounds.pop(0)[1]

        def fake_check(name):
            def check(client, jev_key, spec, usage, only=None, **kw):
                self.finish.append((name, only))
                return spec, ""
            return check

        def fake_notes(client, spec, extras, usage, kept=None):
            self.notes += 1
            return spec, "done"

        self.current = base
        monkeypatch.setattr(deepen, "generate", fake_generate)
        monkeypatch.setattr(deepen, "verify", fake_verify)
        monkeypatch.setattr(deepen, "audit", fake_audit)
        monkeypatch.setattr(deepen, "clarity_check", fake_check("clarity"))
        monkeypatch.setattr(deepen, "fact_check", fake_check("fact"))
        monkeypatch.setattr(deepen, "write_notes", fake_notes)


REQ = {"mode": "concept", "text": "How do transformers work?", "quality": "quality-deep", "effort": "low"}
F = [{"id": "f1", "check": "facts", "stage": "x", "detail": "d"}]


def test_a_clean_page_stops_after_one_verify(monkeypatch):
    loop = Loop(monkeypatch, base_spec(4), [([], [])])
    events: list[dict] = []
    r = deepen.generate_deep(None, REQ, events.append, jev_key="jev")
    assert len(loop.calls) == 1 and loop.verified == [None] and loop.finish == []
    assert [v["label"] for v in r["versions"]] == ["generated", "round 1"]
    assert {"type": "step", "step": "improve", "status": "done", "detail": "1 round · 0 improvements"} in events


def test_a_clean_audit_stops_the_loop(monkeypatch):
    base = base_spec(4)
    loop = Loop(monkeypatch, base, [(F, [{**item(base["stages"][0]["id"]), "verdict": "false_alarm", "action": "none"}])])
    deepen.generate_deep(None, REQ, lambda e: None, jev_key="jev")
    assert len(loop.calls) == 1


def test_rounds_repeat_until_the_audit_is_clean(monkeypatch):
    base = base_spec(4, fromMemory=True, sectionTools=["search"])
    s = [st["id"] for st in base["stages"]]
    monkeypatch.setattr(deepen, "MAX_ROUNDS", 4)
    loop = Loop(monkeypatch, base, [(F, [item(s[0])]), (F, [item(s[1])]), (F, [item(s[2])])])
    r = deepen.generate_deep(None, {**REQ, "extras": ["examples", "depth"]}, lambda e: None, jev_key="jev")
    spec = r["spec"]
    assert len(loop.calls) == 4  # the page, then 3 improves
    assert loop.verified == [None, [0], [1], [2]]  # later rounds look closely only at what changed; round 4 is clean
    assert loop.errors_only == [False, True, True, True]  # round 1 checks everything, later rounds only errors
    assert loop.finish == []  # every change was verified by a later round
    assert [v["label"] for v in r["versions"]] == ["generated", "round 1", "round 2", "round 3", "round 4"]
    improves = loop.calls[1:]
    assert all("Audit items to apply" in c["followUp"]["improve"] and c["extras"] == ["examples"] for c in improves)
    assert [st["blocks"][0]["md"] for st in spec["stages"][:3]] == ["fixed 2", "fixed 3", "fixed 4"]
    assert spec["design"]["textSize"] == "large" and spec["fromMemory"] is True and spec["sectionTools"] == ["search"]
    # Only the final page's own warnings: the earlier versions' (possibly fixed) ones are gone.
    assert "revision" not in spec and "improve 4" in spec["warnings"] and "improve 2" not in spec["warnings"] and loop.notes == 1


def test_a_dismissed_finding_is_not_audited_again(monkeypatch):
    base = base_spec(4)
    s = [st["id"] for st in base["stages"]]
    noise = {"id": "f2", "check": "build", "stage": "", "detail": "Readout goes outside the valid range"}
    first = [{**F[0], "stage": s[0]}, noise]
    dismissed_noise = {"findings": ["f2"], "verdict": "false_alarm", "action": "none", "stage": "", "after": "", "instruction": ""}
    loop = Loop(monkeypatch, base, [(first, [{**item(s[0]), "findings": ["f1"]}, dismissed_noise]), ([{**noise, "id": "f1"}], [])])
    r = deepen.generate_deep(None, REQ, lambda e: None, jev_key="jev")
    assert len(loop.audited) == 1  # round 2 found only the dismissed warning again, so it was clean
    assert r["versions"][2]["findings"] == []


def test_the_safety_cap_ends_with_a_final_check(monkeypatch):
    base = base_spec(4)
    s = [st["id"] for st in base["stages"]]
    monkeypatch.setattr(deepen, "MAX_ROUNDS", 2)
    loop = Loop(monkeypatch, base, [(F, [item(s[0])]), (F, [item(s[1])]), (F, [item(s[2])])])
    r = deepen.generate_deep(None, REQ, lambda e: None, jev_key="jev")
    assert len(loop.calls) == 3 and loop.finish == [("clarity", [1]), ("fact", [1])]
    assert any("safety cap of 2 rounds" in w for w in r["spec"]["warnings"])
    assert r["versions"][-1]["label"] == "final check"


def test_an_improve_that_changes_nothing_stops(monkeypatch):
    base = base_spec(4)
    loop = Loop(monkeypatch, base, [(F, [item(base["stages"][0]["id"])]), (F, [item(base["stages"][1]["id"])])])
    monkeypatch.setattr(deepen, "guard", lambda old, new, items: (old, [], []))
    r = deepen.generate_deep(None, REQ, lambda e: None, jev_key="jev")
    assert len(loop.calls) == 2 and any("changed nothing" in w for w in r["spec"]["warnings"])


def test_a_failed_improve_keeps_the_page_so_far(monkeypatch):
    base = base_spec(4)
    loop = Loop(monkeypatch, base, [(F, [item(base["stages"][0]["id"])])])
    real = deepen.generate

    def flaky(client, req, *a, **k):
        if "followUp" in req:
            raise RuntimeError("overloaded")
        return real(client, req, *a, **k)

    monkeypatch.setattr(deepen, "generate", flaky)
    r = deepen.generate_deep(None, REQ, lambda e: None, jev_key="jev")
    assert r["spec"]["stages"] == base["stages"] and any("improve failed" in w for w in r["spec"]["warnings"])
    assert loop.finish == []


def test_without_jev_only_the_first_page(monkeypatch):
    loop = Loop(monkeypatch, base_spec(4), [(F, [])])
    r = deepen.generate_deep(None, REQ, lambda e: None, jev_key=None)
    assert len(loop.calls) == 1 and loop.verified == [] and any("no Jev key" in w for w in r["spec"]["warnings"])


# ---------- requests ----------


@pytest.mark.parametrize("extras", [["depth", "examples"], []])
def test_requests_accept_quality_deep(extras):
    r = parse_generate({"mode": "concept", "text": "x", "quality": "quality-deep", "extras": extras})
    assert r["quality"] == "quality-deep" and r["extras"] == extras and "debugVersions" not in r
    assert parse_generate({"mode": "concept", "text": "x", "quality": "quality-2"})["quality"] == "quality-plus"


def test_debug_versions_only_for_quality_deep():
    assert parse_generate({"mode": "concept", "text": "x", "quality": "quality-deep", "debugVersions": True})["debugVersions"] is True
    assert "debugVersions" not in parse_generate({"mode": "concept", "text": "x", "quality": "quality-plus", "debugVersions": True})


def test_review_rounds_set_the_limit(monkeypatch):
    base = base_spec(4)
    s = [st["id"] for st in base["stages"]]
    loop = Loop(monkeypatch, base, [(F, [item(s[0])]), (F, [item(s[1])])])
    r = deepen.generate_deep(None, {**REQ, "reviewRounds": 1}, lambda e: None, jev_key="jev")
    assert len(loop.calls) == 2 and loop.finish == [("clarity", [0]), ("fact", [0])]  # one improve, then the final check
    assert any("your limit of 1 review round " in w for w in r["spec"]["warnings"])


def test_more_review_rounds_than_the_default_run_to_clean(monkeypatch):
    base = base_spec(4)
    s = [st["id"] for st in base["stages"]]
    loop = Loop(monkeypatch, base, [(F, [item(s[0])]), (F, [item(s[1])]), (F, [item(s[2])]), (F, [item(s[3])])])
    r = deepen.generate_deep(None, {**REQ, "reviewRounds": 5}, lambda e: None, jev_key="jev")
    assert len(loop.calls) == 5 and loop.finish == []  # 4 improves, and round 5 was clean
    assert not any("limit" in w or "safety cap" in w for w in r["spec"]["warnings"])


@pytest.mark.parametrize("rounds,kept", [(1, 1), (5, 5), (10, 10), (0, None), (11, None), (True, None), ("3", None), (2.5, None)])
def test_requests_accept_review_rounds(rounds, kept):
    r = parse_generate({"mode": "concept", "text": "x", "quality": "quality-deep", "reviewRounds": rounds})
    assert r.get("reviewRounds") == kept
    assert parse_generate({"mode": "concept", "text": "x", "quality": "freeform-plus", "reviewRounds": 3})["reviewRounds"] == 3
    assert "reviewRounds" not in parse_generate({"mode": "concept", "text": "x", "quality": "lite-plus", "reviewRounds": 3})  # not Quick read


def test_a_creative_page_keeps_its_widgets(monkeypatch):
    base = base_spec(4)
    widget = {"type": "widget", "title": "Drag the points", "html": "<div></div>", "height": 300}
    st0 = base["stages"][0]
    base = {**base, "stages": [{**st0, "blocks": [st0["blocks"][0], widget, *st0["blocks"][1:]]}, *base["stages"][1:]]}
    loop = Loop(monkeypatch, base, [(F, [item(st0["id"])]), ([], [])])
    r = deepen.generate_deep(None, {**REQ, "quality": "freeform-plus", "reviewRounds": 3}, lambda e: None, jev_key="jev")
    improve = loop.calls[1]
    assert "(interactive widget: Drag the points)" in improve["followUp"]["outline"] and "Keep every such block" in improve["followUp"]["improve"]
    blocks = r["spec"]["stages"][0]["blocks"]
    assert blocks[0]["md"] == "fixed 2" and blocks[1] == widget  # fixed text, and the real widget back in place
    assert r["usage"]["quality"] == "freeform-plus"


def test_a_lab_page_keeps_its_scenes(monkeypatch):
    base = base_spec(4)
    scene = {"type": "scene", "title": "Spread the votes", "lesson": "x", "state": [], "elements": [], "rules": [], "layout": "stack", "caption": ""}
    st0 = base["stages"][0]
    base = {**base, "stages": [{**st0, "blocks": [st0["blocks"][0], scene, *st0["blocks"][1:]]}, *base["stages"][1:]]}
    loop = Loop(monkeypatch, base, [(F, [item(st0["id"])]), ([], [])])
    r = deepen.generate_deep(None, {**REQ, "quality": "scene-plus", "reviewRounds": 3}, lambda e: None, jev_key="jev")
    improve = loop.calls[1]
    assert "(interactive widget: Spread the votes)" in improve["followUp"]["outline"] and "Keep every such block" in improve["followUp"]["improve"]
    assert r["spec"]["stages"][0]["blocks"][1] == scene
    assert r["usage"]["quality"] == "scene-plus"


def test_a_code_lab_7_page_keeps_its_widgets(monkeypatch):
    base = base_spec(4)
    chart = {"type": "widget", "title": "Rank versus parameters", "html": '<div id="xw-vega"></div><script>XW.vega("xw-vega", {})</script>', "plan": "{}"}
    st0 = base["stages"][0]
    base = {**base, "stages": [{**st0, "blocks": [st0["blocks"][0], chart, *st0["blocks"][1:]]}, *base["stages"][1:]]}
    loop = Loop(monkeypatch, base, [(F, [item(st0["id"])]), ([], [])])
    r = deepen.generate_deep(None, {**REQ, "quality": "code-lab-7", "reviewRounds": 2}, lambda e: None, jev_key="jev")
    assert loop.calls[0]["quality"] == "code-lab-7"  # the first page is a real Code Lab 7.0 page
    improve = loop.calls[1]
    assert "(interactive widget: Rank versus parameters)" in improve["followUp"]["outline"] and "Keep every such block" in improve["followUp"]["improve"]
    assert r["spec"]["stages"][0]["blocks"][1] == chart and r["usage"]["quality"] == "code-lab-7"


def test_review_rounds_are_for_code_lab_7_only_among_the_code_labs():
    ask = lambda q: parse_generate({"mode": "concept", "text": "x", "quality": q, "reviewRounds": 2}).get("reviewRounds")  # noqa: E731
    assert ask("code-lab-7") == 2
    assert ask("code-lab") is None and ask("code-lab-2") is None and ask("code-lab-3") is None


def test_code_lab_notes_are_not_build_findings():
    from explainer import verify as V
    assert "Code Lab (S0): the reviewer rejected the widget".startswith(V.BUILD_SKIP)


def test_widget_stages_are_not_flagged_as_text_only():
    from explainer import verify as V
    spec = base_spec(2)
    only_widget = {**spec["stages"][0], "blocks": [{"type": "text", "md": "Hi."}, {"type": "widget", "title": "W", "html": "<p></p>", "height": 200}]}
    text_only = {**spec["stages"][1], "blocks": [{"type": "text", "md": "Hi."}]}
    page = {**spec, "stages": [only_widget, text_only]}
    flagged = [f["stage"] for f in V.code_checks(page) if "only text" in f["detail"]]
    assert flagged == [text_only["id"]]


# ---------- the day's budget (audit F07) ----------


def test_no_rounds_once_the_budget_is_spent(monkeypatch):
    base = base_spec(4)
    loop = Loop(monkeypatch, base, [(F, [item(base["stages"][0]["id"])])] * 5)
    r = deepen.generate_deep(None, {**REQ, "reviewRounds": 5}, lambda e: None, jev_key="jev", usd_budget=0.0)
    assert loop.verified == [] and len(loop.calls) == 1  # the first page only
    warnings = r["spec"]["warnings"]
    assert any("allowance is used up" in w for w in warnings) and not any("stopped at your limit" in w for w in warnings)


def test_the_improve_waits_for_budget(monkeypatch):
    base = base_spec(4)
    loop = Loop(monkeypatch, base, [(F, [item(base["stages"][0]["id"])])] * 5)
    audit = deepen.audit

    def costly_audit(client, f, spec, findings, effort, usage, later=False):
        usage.add("audit", "claude-haiku-5-5", {"input_tokens": 200_000, "output_tokens": 50_000})  # spends past the budget
        return audit(client, f, spec, findings, effort, usage, later=later)

    monkeypatch.setattr(deepen, "audit", costly_audit)
    r = deepen.generate_deep(None, {**REQ, "reviewRounds": 5}, lambda e: None, jev_key="jev", usd_budget=0.01)
    assert len(loop.audited) == 1 and len(loop.calls) == 1  # audited once, never improved
    assert r["spec"]["stages"] == base["stages"] and any("allowance is used up" in w for w in r["spec"]["warnings"])


def test_a_budget_left_changes_nothing(monkeypatch):
    base = base_spec(4)
    loop = Loop(monkeypatch, base, [(F, [item(base["stages"][0]["id"])]), ([], [])])
    deepen.generate_deep(None, REQ, lambda e: None, jev_key="jev", usd_budget=100.0)
    assert len(loop.calls) == 2 and len(loop.verified) == 2
