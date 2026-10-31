"""Lab mode (scene-plus): the scene pass (scenes.py: Jev gate, plan, build, check, repair) with Haiku and Jev faked,
the plan checks (labplan.py), and the mode's request handling."""

from __future__ import annotations

import json

from explainer import labplan, pipeline, scenes
from explainer.core.validate import assemble_spec
from explainer.pricing import UsageLog
from explainer.requests import parse_generate

SLIDER = {"type": "slider", "var": "B", "label": "Number of models B", "min": 1, "max": 100, "step": 1, "value": 10, "unit": ""}
CHART = {"type": "chart", "title": "Spread", "lesson": "l", "series": [{"label": "m", "expr": "mean(normal(0, 1, B))"}],
         "x": "", "by": "B", "at": [1], "view": "compare", "labels": [], "caption": ""}
SCENE = {
    "title": "Play the models", "lesson": "More models, less spread.", "caption": "", "layout": "stack",
    "state": [{"name": "playing", "label": "Playing", "value": 0, "min": 0, "max": 1, "step": 1},
              {"name": "N", "label": "Models N", "value": 1, "min": 1, "max": 100, "step": 1}],
    "elements": [{"id": "n", "kind": "slider", "var": "N"},
                 {"id": "play", "kind": "button", "label": "Play", "disabled": "playing"},
                 {"id": "c", "kind": "chart", "label": "Spread", "series": [{"label": "m", "expr": "mean(normal(0, 1, N))"}], "by": "N", "at": [1], "view": "compare"}],
    "rules": [{"when": "click", "target": "play", "set": [{"var": "N", "expr": "1"}, {"var": "playing", "expr": "1"}]},
              {"when": "every", "ms": 60, "if": "playing", "set": [{"var": "N", "expr": "N + 1"}]},
              {"when": "change", "target": "N", "if": "N >= 100", "set": [{"var": "playing", "expr": "0"}]}],
}
PLAN = {"build": True, "lesson": "More models, less spread.", "pattern": "play", "readerDoes": "Press Play and watch the spread shrink.",
        "variables": [{"name": "playing", "label": "Playing", "min": 0, "max": 1, "step": 1, "value": 0, "role": "rule"},
                      {"name": "N", "label": "Models N", "min": 1, "max": 100, "step": 1, "value": 1, "role": "reader"}],
        "buttons": [{"id": "play", "label": "Play", "does": "runs N from 1 to 100"}],
        "shows": [{"id": "c", "kind": "chart", "what": "baseline vs now histograms of the average"}],
        "expects": [{"var": "N", "changes": ["c"]}], "replaces": [1, 2]}
NO_PLAN = {**PLAN, "build": False}
NO_KEY = "Lab: no Jev key, so sections with a formula, control or chart were given labs."


def outline(*stages: list[dict]) -> dict:
    return {"title": "Bagging", "subtitle": "", "level": "student", "layout": "story", "keyIntuition": "k",
            "phases": [{"id": "p", "label": "P"}], "glossary": [],
            "stages": [{"id": f"s{i}", "label": f"S{i}", "phase": "p", "blurb": "", "blocks": b} for i, b in enumerate(stages)]}


def fake_haiku(monkeypatch, plans: list, builds: list) -> dict[str, list]:
    """Plan calls return the next plan; build and repair calls the next scene reply (a dict as JSON, or raw text).
    Records what each kind of call was sent."""
    sent: dict[str, list] = {"lab plan": [], "scene": [], "scene repair": []}
    plan_it, build_it = iter(plans), iter(builds)

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        if step == "lab plan":
            assert "You plan" in system and kw.get("fmt")
        else:
            assert "following a plan" in system
        sent[step].append(json.loads(user))
        r = next(plan_it if step == "lab plan" else build_it)
        return r if isinstance(r, str) else json.dumps(r)

    monkeypatch.setattr(scenes, "write_text", write_text)
    return sent


def fake_jev(monkeypatch, by_label: dict[str, dict]) -> list[dict]:
    """Jev answers by section label ({"varies": p, ...}); records the states it was sent."""
    states: list[dict] = []

    def jev_ask(key, state, questions):
        states.append(state)
        p = by_label[state["section"]["label"]]
        return {"answers": {k: {"type": "noul", "p": p.get(k, 0.0)} for k in questions}, "model": "jev-test",
                "usage": {"input_tokens": 100, "output_tokens": 0}}

    monkeypatch.setattr(scenes, "jev_ask", jev_ask)
    return states


def test_a_lab_that_passes_replaces_the_parts_it_names(monkeypatch):
    o = outline([{"type": "text", "md": "Why averaging helps."}, SLIDER, CHART, {"type": "quiz", "questions": []}])
    sent = fake_haiku(monkeypatch, [PLAN], [{"scene": SCENE, "replaces": [1, 2, 0]}])
    notes: list[str] = []
    out, stats = scenes.design_scenes(None, o, UsageLog("scene-plus"), notes)
    assert stats == {"sections": 1, "opened": 1, "planned": 1, "repaired": 0, "placed": 1} and notes == [NO_KEY]
    # The text (index 0) is never replaced; the slider and chart become the lab, in their place.
    assert [b["type"] for b in out["stages"][0]["blocks"]] == ["text", "scene", "quiz"]
    assert sent["lab plan"][0]["section"]["blocks"][1]["index"] == 1 and sent["lab plan"][0]["pageControls"][0]["var"] == "B"
    assert sent["scene"][0]["plan"]["expects"] == PLAN["expects"]  # the builder gets the checked plan
    spec = assemble_spec(out, None, "")
    assert not [w for w in spec["warnings"] if "Scene" in w or "scene" in w]


def test_a_control_another_section_reads_stays(monkeypatch):
    o = outline([SLIDER, CHART], [{"type": "readout", "items": [{"label": "sd", "expr": "1 / sqrt(B)", "digits": 2}]}])
    # The kept slider B would move nothing in its own section, so the lab is sent back once and the repair reads B.
    reads_b = {**SCENE, "rules": [*SCENE["rules"][:2], {**SCENE["rules"][2], "if": "playing * (N < B)"}]}
    sent = fake_haiku(monkeypatch, [PLAN, NO_PLAN], [{"scene": SCENE, "replaces": [0, 1]}, {"scene": reads_b, "replaces": [0, 1]}])
    out, stats = scenes.design_scenes(None, o, UsageLog("scene-plus"), [])
    assert stats["placed"] == 1 and stats["repaired"] == 1
    assert "would no longer move anything in this section" in json.dumps(sent["scene repair"][0])
    assert [b["type"] for b in out["stages"][0]["blocks"]] == ["slider", "scene"]


def test_no_plan_a_failing_lab_or_no_answer_keeps_the_section(monkeypatch):
    o = outline([SLIDER, CHART], [SLIDER, CHART], [SLIDER, CHART], [{"type": "text", "md": "only reading"}])
    broken = {**SCENE, "elements": [{"id": "n", "kind": "slider", "var": "N"}]}  # shows nothing: rejected, and again after the repair
    # Plans run in parallel but replies are matched by order of call, so every section gets an answer of the same kind.
    sent = fake_haiku(monkeypatch, [NO_PLAN, PLAN, PLAN], [{"scene": broken, "replaces": [0, 1]}] * 4)  # 2 builds + 2 repairs
    notes: list[str] = []
    out, stats = scenes.design_scenes(None, o, UsageLog("scene-plus"), notes)
    assert stats["placed"] == 0 and stats["planned"] == 2 and out == o
    assert len(sent["lab plan"]) == 3  # without Jev, the reading-only section isn't planned
    failed = [n for n in notes if "didn't do what its plan promised" in n]
    assert len(failed) == 2 and all("nothing in it shows anything" in n for n in failed)


def test_a_builder_that_doesnt_answer_keeps_the_section(monkeypatch):
    o = outline([SLIDER, CHART])
    fake_haiku(monkeypatch, [PLAN], ["not json at all"])
    notes: list[str] = []
    out, stats = scenes.design_scenes(None, o, UsageLog("scene-plus"), notes)
    assert out == o and notes[-1] == "Lab (S0): the lab builder didn't answer, so the section keeps its parts."


def test_jev_opens_a_text_only_section_and_skips_a_static_one(monkeypatch):
    process = [{"type": "text", "md": "Boosting adds one weak learner per round."}, {"type": "quiz", "questions": []}]
    static = [{"type": "text", "md": "Use boosting when the model underfits."}]
    o = outline(process, static)
    states = fake_jev(monkeypatch, {"S0": {"process": 0.9, "static": 0.1}, "S1": {"varies": 0.3, "static": 0.9}})
    sent = fake_haiku(monkeypatch, [{**PLAN, "replaces": []}], [{"scene": SCENE, "replaces": []}])
    usage = UsageLog("scene-plus")
    notes: list[str] = []
    out, stats = scenes.design_scenes(None, o, usage, notes, jev_key="k")
    assert stats == {"sections": 2, "opened": 1, "planned": 1, "repaired": 0, "placed": 1} and notes == []
    assert sorted(s["section"]["label"] for s in states) == ["S0", "S1"]
    assert "process" in sent["lab plan"][0]["hint"]
    # Added beside the text, before the closing quiz; the static section is untouched.
    assert [b["type"] for b in out["stages"][0]["blocks"]] == ["text", "scene", "quiz"] and out["stages"][1] == o["stages"][1]
    assert any(st["step"] == "jev lab gate" for st in usage.summary()["steps"])


def test_a_broken_promise_gets_one_repair(monkeypatch):
    o = outline([SLIDER, CHART])
    # The chart ignores N (a fixed formula), so "moving N changes c" is false until the repair.
    flat = {**SCENE, "elements": [*SCENE["elements"][:2], {**SCENE["elements"][2], "series": [{"label": "m", "expr": "mean(normal(0, 1, 10))"}]}]}
    sent = fake_haiku(monkeypatch, [PLAN], [{"scene": flat, "replaces": [0, 1]}, {"scene": SCENE, "replaces": [0, 1]}])
    out, stats = scenes.design_scenes(None, o, UsageLog("scene-plus"), [])
    assert stats["repaired"] == 1 and stats["placed"] == 1
    assert len(sent["scene repair"]) == 1
    assert any('doesn\'t change "c"' in p for p in sent["scene repair"][0]["problems"])


def test_plan_checks():
    _, problems = labplan.check_plan({**PLAN, "expects": []}, set(), 3)
    assert problems == ['reader variable "N" has no expects entry']
    # A variable named like a page control becomes that control (read, not copied); a "page" variable must exist.
    plan, problems = labplan.check_plan({**PLAN, "variables": [{**PLAN["variables"][1], "name": "B"}], "expects": [{"var": "B", "changes": ["c"]}]}, {"B"}, 3)
    assert problems == [] and plan["variables"][0]["role"] == "page"
    _, problems = labplan.check_plan({**PLAN, "variables": [{"name": "eta", "role": "page"}], "expects": [{"var": "eta", "changes": ["c"]}]}, {"B"}, 3)
    assert problems == ['variable "eta" isn\'t a page control', 'expects names "eta", which isn\'t a plan variable']
    assert labplan.check_plan(NO_PLAN, set(), 3) == (None, [])
    plan, problems = labplan.check_plan({**PLAN, "replaces": [1, 7, True]}, set(), 3)
    assert problems == [] and plan["replaces"] == [1]


def test_a_lines_chart_follows_its_by_slider():
    # The audited bagging lab: rho on a lines chart's by axis. With the "now" line, moving rho moves the chart.
    assert labplan.lines_values([0, 0.3, 0.7], 0.5) == [0, 0.3, 0.7, 0.5]
    assert labplan.lines_values([0, 0.3, 0.7], 0.3) == [0, 0.3, 0.7]
    scene = {"defs": [], "state": [{"name": "rho", "label": "r", "value": 0.3, "min": 0, "max": 1, "step": 0.05},
                                   {"name": "B", "label": "B", "value": 25, "min": 1, "max": 100, "step": 1}],
             "elements": [{"id": "r", "kind": "slider", "var": "rho"}, {"id": "b", "kind": "slider", "var": "B"},
                          {"id": "c", "kind": "chart", "series": [{"label": "v", "expr": "rho + (1 - rho) / B"}], "x": "B", "by": "rho", "at": [0, 0.3, 0.7], "view": "lines"}]}
    plan = {"variables": [], "shows": [], "buttons": [], "expects": []}
    assert labplan.check_build(scene, plan, {"seed": 0}, {}) == []
    # Fixed lines at both ends of rho's range: the chart only moves in between, which still counts.
    ends = {**scene, "elements": [*scene["elements"][:2], {**scene["elements"][2], "at": [0, 0.5, 1]}]}
    assert labplan.check_build(ends, plan, {"seed": 0}, {}) == []
    # Drawn by a variable its formula never reads: every line is the same.
    same = {**scene, "elements": [*scene["elements"][:2], {**scene["elements"][2], "series": [{"label": "v", "expr": "1 / B"}]}]}
    assert labplan.check_build(same, plan, {"seed": 0}, {})[0].startswith('chart "c" is drawn by rho (lines view), but its formula never reads rho')
    # A readout that reads rho but can't change with it is caught by the slider rule.
    stuck = {**scene, "elements": [*scene["elements"], {"id": "o", "kind": "readout", "expr": "rho * 0"}]}
    assert labplan.check_build(stuck, plan, {"seed": 0}, {}) == ['moving rho from 0 to 1 doesn\'t change "o"']
    # A lab slider that copies a page slider (same label) is sent back.
    copy = {**scene, "elements": [{**scene["elements"][0], "label": "Correlation"}, *scene["elements"][1:]]}
    assert labplan.check_build(copy, plan, {"seed": 0, "corr": 0.3}, {"corr": (0, 1)}, {"corr": "Correlation"}) == [
        'slider "r" copies the page\'s slider for corr: read corr by name instead of a new variable']
    # A page variable in the plan is moved over the page slider's range.
    reads_page = {**scene, "elements": [*scene["elements"], {"id": "p", "kind": "readout", "expr": "corr * 2"}]}
    page_plan = {**plan, "variables": [{"name": "corr", "role": "page"}], "expects": [{"var": "corr", "changes": ["p"]}]}
    assert labplan.check_build(reads_page, page_plan, {"seed": 0, "corr": 0.3}, {"corr": (0, 1)}) == []


def test_the_lab_mode_request():
    r = parse_generate({"mode": "concept", "text": "bagging", "quality": "scene-plus", "extras": ["examples"], "reviewRounds": 3})
    assert r["quality"] == "scene-plus" and r["extras"] == ["examples"] and r["reviewRounds"] == 3


def test_lab_mode_always_plans_with_the_chart_prompt(monkeypatch):
    monkeypatch.setenv("CHART_BLOCK", "0")
    assert "- chart: use it" not in pipeline.planner_system()
    assert "- chart: use it" in pipeline.planner_system(force_chart=True)


def test_the_new_chart_views_follow_their_variables():
    # band, box, scatter and grouped bars: Lab's build check sees them move when their variable moves.
    state = [{"name": "N", "label": "Models N", "value": 10, "min": 1, "max": 100, "step": 1},
             {"name": "T", "label": "Temperature", "value": 1, "min": 0.1, "max": 3, "step": 0.1}]
    plan = {"variables": [], "shows": [], "buttons": [], "expects": []}

    def scene(*charts: dict) -> dict:
        return {"defs": [], "state": state, "elements": [{"id": "n", "kind": "slider", "var": "N"}, {"id": "t", "kind": "slider", "var": "T"}, *charts]}

    spread = [{"label": "m", "expr": "mean(normal(0, 1, N))"}]
    band = {"id": "band", "kind": "chart", "series": spread, "x": "N", "view": "band"}
    box = {"id": "box", "kind": "chart", "series": spread, "by": "N", "at": [1, 50], "view": "box"}
    dots = {"id": "dots", "kind": "chart", "series": [{"label": "y", "expr": "normal(0, T)"}], "x": "N", "view": "scatter"}
    pairs = {"id": "pairs", "kind": "chart", "series": [{"label": "a", "expr": "normal(0, 1)"}, {"label": "b", "expr": "normal(0, T)"}], "view": "scatter"}
    grouped = {"id": "g", "kind": "chart", "series": [{"label": "1", "expr": "softmax([2, 1, 0], 1)"}, {"label": "T", "expr": "softmax([2, 1, 0], T)"}], "view": "groupedbars"}
    assert labplan.check_build(scene(band, box, dots, pairs, grouped), plan, {"seed": 0}, {}) == []
    # A box chart drawn by a variable its formula never reads draws the same boxes.
    flat_box = {**box, "series": [{"label": "m", "expr": "normal(0, T)"}]}
    assert labplan.check_build(scene(flat_box), plan, {"seed": 0}, {})[0].startswith('chart "box" is drawn by N (box view), but its formula never reads N')
    # Grouped bars whose second list ignores T: moving T changes nothing.
    stuck = {**grouped, "series": [grouped["series"][0], {"label": "T", "expr": "softmax([2, 1, 0], 1) + T * 0"}]}
    assert labplan.check_build(scene(stuck), plan, {"seed": 0}, {})[0].startswith('moving T from 0.1 to 3 doesn\'t change "g"')


def test_the_new_chart_views_in_the_python_checker():
    def chart(**over) -> dict:
        return {"type": "chart", "title": "c", "lesson": "l", "series": [{"label": "d", "expr": "normal(B, 1)"}],
                "x": "", "by": "", "at": [], "view": "line", "labels": [], "caption": "", **over}

    def views(*blocks: dict) -> tuple[list[str], list[str]]:
        spec = assemble_spec(outline([{"type": "text", "md": "x"}, SLIDER, *blocks]), None, "")
        return [b["view"] for st in spec["stages"] for b in st["blocks"] if b["type"] == "chart"], spec["warnings"]

    two = [{"label": "a", "expr": "normal(0, 1)"}, {"label": "b", "expr": "normal(0, B)"}]
    assert views(chart(x="B", view="scatter"), chart(series=two, view="scatter"), chart(x="B", view="band"), chart(by="B", view="box"))[0] == ["scatter", "scatter", "band", "box"]
    got, warnings = views(chart(view="scatter"), chart(view="band"), chart(x="B", view="box"))
    assert got == ["histogram", "histogram", "histogram"]
    assert 'Chart "c": the "scatter" view doesn\'t fit a random formula; showing "histogram".' in warnings
    lists = [{"label": "1", "expr": "softmax([1, 2], 1)"}, {"label": "B", "expr": "softmax([1, 2], B)"}]
    assert views(chart(series=lists, view="groupedbars"), chart(series=lists[1:], view="groupedbars"))[0] == ["groupedbars", "bars"]
    box = assemble_spec(outline([SLIDER, chart(by="B", view="box")]), None, "")["stages"][0]["blocks"][1]
    assert box["at"] == [1, 50.5, 100]


def test_a_contrast_must_be_drawn_together_in_one_part():
    # The lesson compares two things: its contrast part must show both, not one curve next to two readouts.
    state = [{"name": "q", "label": "Model q", "value": 0.4, "min": 0.01, "max": 0.99, "step": 0.01}]
    one = {"id": "c", "kind": "chart", "series": [{"label": "forward", "expr": "q * 2"}], "x": "q", "view": "line"}
    two = {**one, "series": [*one["series"], {"label": "reverse", "expr": "q * 3"}]}
    readouts = [{"id": "f", "kind": "readout", "expr": "q * 2"}, {"id": "r", "kind": "readout", "expr": "q * 3"}]
    plan = {"variables": [], "shows": [], "buttons": [], "expects": [], "contrast": {"items": ["forward", "reverse"], "part": "c"}}

    def build(chart: dict) -> list[str]:
        return labplan.check_build({"defs": [], "state": state, "elements": [{"id": "s", "kind": "slider", "var": "q"}, chart, *readouts]}, plan, {"seed": 0}, {})

    assert build(one)[0].startswith('the lesson compares forward and reverse, but "c" shows only one thing')
    assert build(two) == []
    # No contrast in the plan: one curve is fine.
    assert labplan.check_build({"defs": [], "state": state, "elements": [{"id": "s", "kind": "slider", "var": "q"}, one]}, {**plan, "contrast": {"items": [], "part": ""}}, {"seed": 0}, {}) == []
    # The plan must name a real visual part for its contrast.
    _, problems = labplan.check_plan({**PLAN, "contrast": {"items": ["a", "b"], "part": "nope"}}, set(), 3)
    assert 'the contrast is shown by "nope", which isn\'t a visual part' in problems
    # An older plan without a contrast still works.
    plan_ok, problems = labplan.check_plan(PLAN, set(), 3)
    assert problems == [] and plan_ok["contrast"] == {"items": [], "part": ""}


def test_reader_text_never_uses_the_designers_terms():
    state = [{"name": "q", "label": "Model q", "value": 0.4, "min": 0.01, "max": 0.99, "step": 0.01}]
    els = [{"id": "s", "kind": "slider", "var": "q"}, {"id": "o", "kind": "readout", "label": "Value", "expr": "q * 2"}]
    plan = {"variables": [], "shows": [], "buttons": [], "expects": []}
    scene = {"defs": [], "state": state, "elements": els, "title": "Lab", "caption": "Drag q (page control) to see it change."}
    assert labplan.check_build(scene, plan, {"seed": 0}, {}) == [
        'the lab\'s caption says "page control", which readers don\'t know: name the slider or part by what it shows instead (e.g. "the temperature slider above")']
    assert labplan.check_build({**scene, "caption": "Drag the q slider above to see it change."}, plan, {"seed": 0}, {}) == []


def test_a_lab_must_not_leave_its_sections_slider_moving_nothing():
    # The section's chart reads its slider B. A play lab that counts with its own k and replaces the chart would leave
    # B (and the worked example that sets it) moving nothing in this section.
    chart = {**CHART, "by": "B"}
    example = {"type": "examples", "title": "", "items": [{"title": "Many", "set": [{"var": "B", "value": 100}], "focus": "f"}]}
    o = outline([{"type": "text", "md": "x"}, SLIDER, chart, example])
    counts_k = {**SCENE, "elements": [*SCENE["elements"][:2], {**SCENE["elements"][2], "series": [{"label": "m", "expr": "mean(normal(0, 1, N))"}]}]}
    assert scenes.orphaned(o, 0, counts_k, [2]) == [
        'the section\'s slider "Number of models B" would no longer move anything in this section: read B in the lab '
        '(for example as where a Play counter stops, as a chart\'s x, or in a readout)']
    # Reading B (here: where Play stops) keeps it alive; so does keeping the chart (nothing replaced).
    stops_at_b = {**counts_k, "rules": [*counts_k["rules"][:2], {**counts_k["rules"][2], "if": "playing * (N < B)"}]}
    assert scenes.orphaned(o, 0, stops_at_b, [2]) == []
    assert scenes.orphaned(o, 0, counts_k, []) == []
    # Labels and captions that merely mention B don't count as reading it.
    assert "B" not in scenes.formula_vars({**counts_k, "caption": "Drag B", "title": "B"})
