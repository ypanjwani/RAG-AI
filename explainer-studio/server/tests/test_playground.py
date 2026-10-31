"""Playground mode (playground, codelab._design_playground): Code Lab 5.0's writer marks and Jev checks, but each marked
section's interactive part is a playground spec (shared/playground, from the ai-playground experiment), dry-run by
scripts/check-playground.mjs and repaired from its errors, then reviewed by Jev. Claude and Jev are faked; the node
checker is real."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from explainer import codelab
from explainer.pricing import UsageLog
from explainer.requests import parse_generate

from test_codelab import FIXABLE, GOOD_REVIEW, HOPELESS, fake_jev, marked

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "playground"
GINI = json.loads((FIXTURES / "gini-impurity.json").read_text(encoding="utf-8"))
needs_node = pytest.mark.skipif(not shutil.which("node") or not codelab.PG_SCRIPT.exists(), reason="node not available")

# A slider whose key isn't in state: the validator rejects it with a fixable message.
BROKEN = {"title": "Broken", "state": {"a": 1},
          "components": [{"id": "s", "type": "slider", "label": "B", "key": "b", "min": 0, "max": 5},
                         {"id": "r", "type": "readout", "label": "A", "value": "return state.a;"}]}


@pytest.fixture(autouse=True)
def no_screenshots(monkeypatch):
    """The screenshot check (PG_LOOK) is off by default but can be turned on; these tests take no screenshots unless a test stubs them in."""
    monkeypatch.setattr(codelab, "snap_widget", lambda html: None)


def fake_haiku(monkeypatch, replies: list) -> list[dict]:
    """Haiku's playground calls: each reply is a spec (dict, sent as JSON) or raw text. Records every call."""
    sent: list[dict] = []
    queue = list(replies)

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        sent.append({"step": step, "effort": kw["effort"], "system": system, "fmt": kw.get("fmt"), "user": json.loads(user),
                     "thinking": kw.get("thinking", True)})
        r = queue.pop(0)
        return r if isinstance(r, str) else json.dumps(r)

    monkeypatch.setattr(codelab, "write_text", write_text)
    return sent


def run(monkeypatch, replies: list, reviews=None, effort: str = "medium", think: bool = True, jev_key: str | None = "k"):
    sent = fake_haiku(monkeypatch, replies)
    asked = fake_jev(monkeypatch, reviews=reviews)
    out = marked({"chart": True, "idea": "move the split and watch the impurity"})
    st = codelab.Streamed(None, UsageLog("playground", effort), jev_key, codelab.CodeLabSettings.for_effort(effort, think=think, playground=True))
    st.feed(json.dumps(out))
    notes: list[str] = []
    result, stats = st.finish(out, notes)
    return result, stats, notes, sent, asked


def widgets(result: dict) -> list[dict]:
    return [b for st in result["stages"] for b in st["blocks"] if b.get("type") == "widget"]


def test_settings_are_code_lab_7_with_playground_parts():
    s5 = codelab.CodeLabSettings.for_effort("high", free=True)
    sp = codelab.CodeLabSettings.for_effort("high", playground=True)
    assert sp.playground is True and s5.playground is False
    assert (sp.plan, sp.build, sp.fix) == ("high", "high", "high")  # the page's Effort everywhere
    assert sp.marks and sp.fix_first and not sp.free
    assert sp.look is codelab.PG_LOOK  # the screenshot check, off unless PG_LOOK=1


def test_is_a_request():
    req = parse_generate({"mode": "concept", "text": "Attention", "quality": "playground", "planThinking": False, "reviewRounds": 2,
                          "debugRejected": True})
    assert req["quality"] == "playground" and req["planThinking"] is False and req["reviewRounds"] == 2 and req["debugRejected"] is True


def test_html_wraps_the_spec_and_escapes_script_ends():
    spec = {**GINI, "title": "</script><b>x", "afterBlock": 0}
    html = codelab.playground_html(spec)
    assert html.startswith('<div id="pg"></div><script>XW.playground(') and html.endswith(")</script>")
    assert html.count("</script>") == 1 and "afterBlock" not in html
    assert json.loads(html[len('<div id="pg"></div><script>XW.playground('):-len(")</script>")].replace("<\\/", "</"))["title"] == "</script><b>x"


@needs_node
def test_a_valid_playground_is_placed_and_reviewed(monkeypatch):
    result, stats, notes, sent, asked = run(monkeypatch, [GINI], think=False)
    assert [s["step"] for s in sent] == ["playground write"]
    assert sent[0]["effort"] == "medium" and sent[0]["thinking"] is False and sent[0]["fmt"] is None
    assert "You design the interactive playground for one section" in sent[0]["system"]
    assert sent[0]["user"]["hint"] == "move the split and watch the impurity" and sent[0]["user"]["section"]["label"] == "S0"
    (w,) = widgets(result)
    assert "XW.playground(" in w["html"] and w["title"] == "Gini Impurity"
    stored = json.loads(w["plan"])
    assert stored["lesson"].startswith("A split is good") and stored["review"]["controls_on_topic"] == 0.9
    # Jev read the checker's facts: the controls and what each one changes
    review = asked.reviews[0]
    assert {c["label"] for c in review["widget"]["controls"]} >= {"Split at", "New data"}
    assert any(c["control"] == "Split at" and "Gini" in c["changes"] for c in review["widget"]["changes"])
    assert stats["built"] == 1 and not any(n.startswith("Playground") for n in notes)


@needs_node
def test_a_failing_playground_is_repaired_with_the_checkers_errors(monkeypatch):
    result, stats, notes, sent, _ = run(monkeypatch, [BROKEN, GINI])
    assert [s["step"] for s in sent] == ["playground write", "playground repair"]
    repair = sent[1]
    assert "fix your playground" in repair["system"] and repair["user"]["yourPreviousSpec"]["title"] == "Broken"
    assert any('key "b" is not in state' in p for p in repair["user"]["problems"])
    assert len(widgets(result)) == 1 and stats["repaired"] == 1


@needs_node
def test_still_failing_after_the_repairs_stays_text(monkeypatch):
    result, stats, notes, sent, _ = run(monkeypatch, [BROKEN] * (1 + codelab.MAX_REPAIRS))
    assert len(sent) == 1 + codelab.MAX_REPAIRS and not widgets(result)
    assert any("failed its checks and couldn't be repaired" in n for n in notes)


@needs_node
def test_no_playground_answer_stays_text(monkeypatch):
    result, _, notes, sent, _ = run(monkeypatch, ['{"error": "no_playground"}'])
    assert len(sent) == 1 and not widgets(result)
    assert any("found nothing to play with" in n for n in notes)


def test_the_check_fails_closed_without_node(monkeypatch):
    monkeypatch.setattr(codelab, "_node", lambda: None)
    assert codelab.check_playground(GINI)["problems"] == [codelab.CHECK_FAILED]
    result, _, notes, sent, _ = run(monkeypatch, [GINI])
    assert len(sent) == 1 and not widgets(result)  # no repair: a repair can't fix a checker that can't run
    # the note says the checker couldn't run, not that the playground failed its checks
    assert any("couldn't run its checker" in n for n in notes) and not any("failed its checks" in n for n in notes)


def test_the_check_fails_closed_when_node_exits_with_nothing(monkeypatch, caplog):
    """Oct 24: node exited at once with no output and no error (a dev server whose console window was gone)."""
    class Silent:
        stdout, stderr, returncode = "", "", -1073741502  # 0xC0000142: the process couldn't initialise
    calls: list[dict] = []
    monkeypatch.setattr(codelab.subprocess, "run", lambda *a, **k: calls.append(k) or Silent())
    monkeypatch.setattr(codelab, "_node", lambda: "node")
    assert codelab.check_playground(GINI)["problems"] == [codelab.CHECK_FAILED]
    assert len(calls) == codelab.CHECK_RUNS  # tried again once
    assert all(k["creationflags"] == codelab._NODE_FLAGS for k in calls)
    assert "0xc0000142" in caplog.text  # the log says why


def test_the_check_fails_closed_when_it_crashes(monkeypatch):
    class Boom:
        stdout, stderr = "not json", "crash"
    monkeypatch.setattr(codelab.subprocess, "run", lambda *a, **k: Boom())
    monkeypatch.setattr(codelab, "_node", lambda: "node")
    assert codelab.check_playground(GINI)["problems"] == [codelab.CHECK_FAILED]


@needs_node
def test_a_snippet_that_never_ends_is_stopped():
    spec = copy.deepcopy(GINI)
    spec["init"] = "while (true) {}\nreturn {};"
    problems = codelab.check_playground(spec)["problems"]
    assert problems and "too long" in problems[0]


@needs_node
def test_a_too_large_spec_is_sent_back_before_running():
    spec = copy.deepcopy(GINI)
    spec["state"]["blob"] = "x" * 41_000
    assert "too long" in codelab.check_playground(spec)["problems"][0]


@needs_node
def test_a_jev_rejection_gets_one_fix(monkeypatch):
    result, stats, notes, sent, asked = run(monkeypatch, [GINI, GINI], reviews=[FIXABLE, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["playground write", "playground repair"]
    assert any("lesson" in p for p in sent[1]["user"]["problems"])
    assert len(widgets(result)) == 1 and len(asked.reviews) == 2


@needs_node
def test_rejected_twice_stays_text(monkeypatch):
    result, _, notes, sent, _ = run(monkeypatch, [GINI, GINI], reviews=[HOPELESS])
    assert len(sent) == 2 and not widgets(result)
    assert any("reviewer rejected the playground" in n for n in notes)


@needs_node
def test_without_jev_a_checked_playground_is_kept(monkeypatch):
    result, stats, _, _, _ = run(monkeypatch, [GINI], jev_key=None)
    assert len(widgets(result)) == 1


@needs_node
def test_the_review_uses_playground_questions_and_marks_buttons_that_only_run_it(monkeypatch):
    # a soft no (between PG_REVIEW_LOW and Code Lab's REVIEW_LOW) is kept and only logged
    result, stats, notes, sent, asked = run(monkeypatch, [GINI], reviews=[{**GOOD_REVIEW, "controls_on_topic": 0.25}])
    assert len(widgets(result)) == 1 and len(sent) == 1
    roles = {c["label"]: c["role"] for c in asked.reviews[0]["widget"]["controls"]}
    assert roles["New data"] == "runs it" and roles["Split at"] == "setting" and roles["Click adds"] == "setting"
    assert "Play, Step" not in str(asked.reviews[0])  # the state is data; the wording lives in the questions
    assert codelab.PG_REVIEW_QUESTIONS["controls_on_topic"] != codelab.REVIEW_QUESTIONS["controls_on_topic"]


@needs_node
def test_a_clear_no_names_the_setting_controls_in_the_repair(monkeypatch):
    result, _, _, sent, _ = run(monkeypatch, [GINI, GINI], reviews=[{**GOOD_REVIEW, "controls_on_topic": 0.1}, GOOD_REVIEW])
    assert [s["step"] for s in sent] == ["playground write", "playground repair"]
    (problem,) = sent[1]["user"]["problems"]
    assert "Split at" in problem and "Click adds" in problem and "New data" not in problem and "renaming it is not enough" in problem
    assert len(widgets(result)) == 1


def test_other_modes_keep_the_code_lab_threshold():
    answers = {k: {"p": v} for k, v in {**GOOD_REVIEW, "controls_on_topic": 0.25}.items()}
    assert codelab.review_verdict(answers, False, True)["reject"] is True
    assert codelab.review_verdict(answers, False, True, codelab.PG_REVIEW_LOW)["reject"] is False


def test_widen_grows_a_range_with_a_margin():
    assert codelab._widen([0, 1], -0.4) == [-0.54, 1.0]
    assert codelab._widen([0, 10], 5) == [0.0, 10.0]
    assert codelab._widen([0, 10], 12) == [0.0, 13.2]
    assert codelab._widen(None, 1) is None and codelab._widen([3, 1], 1) is None


# A draggable point that starts just below the chart (seen Oct 24), otherwise a valid playground.
DRAG_OUT = {"title": "Drag", "state": {"px": 5, "py": -0.4},
            "components": [{"id": "c", "type": "chart", "title": "Point", "x": [0, 10], "y": [0, 1],
                            "draw": "return [point(state.px, state.py, { id: 'p', draggable: true })];",
                            "onDrag": "return { px: pos.x, py: pos.y };"},
                           {"id": "r", "type": "readout", "label": "x", "value": "return round(state.px, 2);"}]}


@needs_node
def test_a_drag_point_outside_the_chart_is_fixed_by_code_without_a_repair(monkeypatch):
    assert any("outside the chart range" in p for p in codelab.check_playground(DRAG_OUT)["problems"])
    result, stats, _, sent, _ = run(monkeypatch, [DRAG_OUT])
    assert [s["step"] for s in sent] == ["playground write"] and stats["repaired"] == 0
    (w,) = widgets(result)
    spec = json.loads(w["html"][len('<div id="pg"></div><script>XW.playground('):-len(")</script>")].replace("<\\/", "</"))
    assert spec["components"][0]["y"][0] < -0.4


@needs_node
def test_a_button_that_changes_nothing_is_removed_by_code(monkeypatch):
    spec = copy.deepcopy(GINI)
    spec["components"].insert(0, {"id": "b1", "type": "button", "label": "Step", "onClick": "return {};"})
    assert any(p.startswith("b1 never changes state") for p in codelab.check_playground(spec)["problems"])
    result, stats, _, sent, _ = run(monkeypatch, [spec])
    assert len(sent) == 1 and stats["repaired"] == 0
    (w,) = widgets(result)
    assert '"b1"' not in w["html"] and '"Split at"' in w["html"]


def test_autofix_never_removes_the_last_control():
    spec = {"title": "One", "state": {"a": 1}, "components": [{"id": "b1", "type": "button", "label": "Go", "onClick": "return {};"},
                                                             {"id": "r", "type": "readout", "label": "A", "value": "return state.a;"}]}
    assert codelab._pg_autofix(spec, ["b1 never changes state; its slot must return a patch that changes a value"]) is None


def test_autofix_does_not_count_a_reset_button_as_a_control():
    # Haiku sometimes adds a Reset button despite the prompt; the validator doesn't count it, so neither may autofix.
    spec = {"title": "One", "state": {"a": 1}, "components": [{"id": "s1", "type": "slider", "label": "A", "key": "a", "min": 0, "max": 2},
                                                             {"id": "rs", "type": "button", "label": "Reset", "action": "reset"},
                                                             {"id": "r", "type": "readout", "label": "B", "value": "return 1;"}]}
    assert codelab._pg_autofix(spec, ["s1 changes [a] but no chart or readout depends on that (directly or through another control)"]) is None


@needs_node
def test_the_repeat_check_reads_the_stored_plan(monkeypatch):
    result, *_ = run(monkeypatch, [GINI])
    s = codelab.chart_summary(widgets(result)[0])
    assert s["title"] == "Gini Impurity" and s["lesson"].startswith("A split is good")
    assert "Split at (0 to 10)" in s["controls"] and "Gini" in s["axes"]


# Shapes and links (box / circle / diamond / node + link): the checker accepts a diagram and names a link to a missing id.
DIAGRAM = {"title": "Diagram", "plan": {"insight": "i", "mechanism": "m", "user_changes": "u", "reacts": "r"},
           "state": {"w": 3},
           "components": [{"id": "s1", "type": "slider", "label": "Width", "key": "w", "min": 1, "max": 5, "step": 1},
                          {"id": "d1", "type": "chart", "title": "Flow", "x": [0, 10], "y": [0, 6],
                           "draw": "return [node('a', 2, 3, { label: 'In' }), box(4, 2, 4 + state.w, 4, { id: 'b', label: 'Layer' }), "
                                   "diamond(8, 1, 9.5, 3, { id: 'c' }), link('a', 'b'), link('b', 'c', { curve: 0.3 }), link('c', 'c')];"}]}


@needs_node
def test_the_checker_accepts_a_diagram_of_shapes_and_links():
    report = codelab.check_playground(DIAGRAM)
    assert report["problems"] == []
    assert "Layer" in report["facts"]["labels"]


@needs_node
def test_the_checker_names_a_link_to_a_missing_id():
    spec = copy.deepcopy(DIAGRAM)
    spec["components"][1]["draw"] = "return [node('a', 2, 3), link('a', 'nope')];"
    problems = codelab.check_playground(spec)["problems"]
    assert any("no shape in this draw has that id" in p for p in problems)


# ---------- soft layout problems: one repair, kept only if better (_pg_fix_layout) ----------

LAYOUT = FIXTURES.parent / "playground-layout"
OVERLAP = json.loads((LAYOUT / "pipeline-overlap.json").read_text(encoding="utf-8"))
SPREAD = json.loads((LAYOUT / "pipeline-fixed.json").read_text(encoding="utf-8"))


@needs_node
def test_the_checker_reports_layout_problems_without_failing():
    report = codelab.check_playground(OVERLAP)
    assert report["problems"] == [] and report["facts"]
    assert any("overlap" in m for m in report["layout"]) and any("sticks out" in m for m in report["layout"])
    assert codelab.check_playground(SPREAD)["layout"] == []


@needs_node
def test_a_better_layout_repair_is_kept(monkeypatch):
    result, _, notes, sent, _ = run(monkeypatch, [OVERLAP, SPREAD])
    assert [s["step"] for s in sent] == ["playground write", "playground layout"]
    problems = sent[1]["user"]["problems"]
    assert problems[0] == codelab.LAYOUT_LEAD and any("'rr' and 'pk' overlap" in p for p in problems[1:])
    assert sent[1]["user"]["yourPreviousSpec"]["title"] == "Pipeline"
    (w,) = widgets(result)
    assert "node('rr', 7, 4.5" in w["html"]  # the spread-out version
    assert not any(n.startswith("Playground") for n in notes)


@needs_node
def test_a_layout_repair_that_is_no_better_keeps_the_original(monkeypatch):
    result, _, _, sent, _ = run(monkeypatch, [OVERLAP, OVERLAP])
    assert [s["step"] for s in sent] == ["playground write", "playground layout"]
    (w,) = widgets(result)
    assert "node('rr', 5.6, 3" in w["html"]  # still placed: a layout problem never costs the playground


@needs_node
def test_a_layout_repair_that_breaks_a_check_keeps_the_original(monkeypatch):
    result, _, _, sent, _ = run(monkeypatch, [OVERLAP, BROKEN])
    assert [s["step"] for s in sent] == ["playground write", "playground layout"]
    (w,) = widgets(result)
    assert "node('rr', 5.6, 3" in w["html"]


# ---------- layout problems fixed by code before Haiku (_pg_layout_autofix) ----------

def chart(cid: str, draw: str, **kw) -> dict:
    return {"id": cid, "type": "chart", "title": kw.pop("title", "Chart"), "draw": draw, **kw}


def with_charts(*charts: dict) -> dict:
    spec = copy.deepcopy(GINI)
    spec["components"].extend(copy.deepcopy(list(charts)))
    return spec


HEAT = chart("hm", "return [heatmap([[state.t, 1], [2, 3]], { x: [0, 2], y: [0, 2] })];", title="Weights", x=[0, 2], y=[0, 2], yLabel="query")
CAP10 = chart("cap", "return [line(range(11).map((i) => [i, Math.min(10, state.t * i)]))];", x=[0, 10], y=[0, 10])
# the data fills only [0, 50] of x [0, 200], but the draggable point starts at x = 75: narrowing x would cut it off
FAR = chart("far", "return [line(range(11).map((i) => [i * 5, state.t])), point(state.t * 15, 5, { id: 'p', draggable: true })];",
            x=[0, 200], y=[0, 10], onDrag="return { t: clamp(pos.x / 15, 0, 10) };")


LATER = " (after the controls move, not at the start)"  # the validator's mark on a later-state problem


def by_id(spec: dict, cid: str) -> dict:
    return next(c for c in spec["components"] if c.get("id") == cid)


def test_layout_autofix_moves_a_heatmap_axis_name_into_the_title():
    msg = "hm: the y-axis name 'query' is drawn over the heatmap's top-left cells; use yLabels to name the rows and leave yLabel out"
    fixed = codelab._pg_layout_autofix(with_charts(HEAT), [msg])
    assert "yLabel" not in by_id(fixed, "hm") and by_id(fixed, "hm")["title"] == "Weights (rows: query)"
    untitled = with_charts({k: v for k, v in HEAT.items() if k != "title"})
    assert by_id(codelab._pg_layout_autofix(untitled, [msg + LATER]), "hm")["title"] == "Rows: query"


def test_layout_autofix_narrows_an_empty_x_range_but_not_below_its_old_start():
    spec = with_charts(chart("c", "return [];", x=[0, 200], y=[0, 1]))
    msg = "c: the data never fills more than 25% of the x range [0,200] in any state, so most of the chart stays empty; narrow x to about [-2.5, 52.5]"
    assert by_id(codelab._pg_layout_autofix(spec, [msg]), "c")["x"] == [0, 52.5]
    spec = with_charts(chart("c", "return [];", x=[-3, 3], y=[0, 1]))
    msg = "c: the data never fills more than 33% of the x range [-3,3] in any state, so most of the chart stays empty; narrow x to about [-1.1, 1.1]"
    assert by_id(codelab._pg_layout_autofix(spec, [msg]), "c")["x"] == [-1.1, 1.1]


def test_layout_autofix_narrows_an_empty_y_range_the_same_way():
    spec = with_charts(chart("c", "return [];", x=[0, 1], y=[0, 1.2]))
    msg = "c: the data never fills more than 28% of the y range [0,1.2] in any state, so most of the chart stays empty; narrow y to about [-0.0169, 0.366]" + LATER
    fixed = by_id(codelab._pg_layout_autofix(spec, [msg]), "c")
    assert fixed["y"] == [0, 0.366] and fixed["x"] == [0, 1]


@needs_node
def test_the_checker_reports_an_empty_y_range_and_code_fixes_it():
    tiny = with_charts(chart("tiny", "return [area(range(11).map((i) => [i, 0.03 * state.t * Math.exp(-((i - 5) ** 2) / 8)]))];",
                             x=[0, 10], y=[0, 1]))
    layout = codelab.check_playground(tiny)["layout"]
    assert any("of the y range [0,1]" in m for m in layout)
    fixed = codelab._pg_layout_autofix(tiny, layout)
    again = codelab.check_playground(fixed)
    assert not again["problems"] and not any("y range" in m for m in again["layout"])


def test_layout_autofix_gives_a_probability_curve_headroom_but_leaves_a_clamp_to_haiku():
    capped = "c: a line mark sits on the top edge of the chart (y = {top}) for 27 of 50 points, so its values look capped and cut off; don't clamp the values: widen y to fit them, or give it its own chart"
    spec = with_charts(chart("c", "return [];", x=[0, 1], y=[0, 1]))
    assert by_id(codelab._pg_layout_autofix(spec, [capped.format(top=1) + LATER]), "c")["y"] == [0, 1.05]
    spec = with_charts(chart("c", "return [];", x=[0, 1], y=[0, 10]))
    assert codelab._pg_layout_autofix(spec, [capped.format(top=10)]) is None


def test_layout_autofix_widens_y_only_a_little():
    out = "c: a line mark lies outside the y range [-4,4] for 80 of 121 points (values {lo} to {hi}), so the reader sees it cut off at the edge; widen y to fit it, or give it its own chart"
    spec = with_charts(chart("c", "return [];", x=[0, 1], y=[-4, 4]))
    y = by_id(codelab._pg_layout_autofix(spec, [out.format(lo=-6, hi=6)]), "c")["y"]
    assert y[0] < -6 and y[1] > 6
    assert codelab._pg_layout_autofix(spec, [out.format(lo=1.5, hi=192000)]) is None  # would squash the chart


def test_layout_autofix_fits_the_range_to_a_heatmap():
    msg = "c: the heatmap covers x [0,12] but the chart's x range is [0,10], so its outer cells are cut off; set the chart's x to [0,12] or give the heatmap x: [...] inside the range"
    spec = with_charts(chart("c", "return [];", x=[0, 10], y=[0, 6]))
    assert by_id(codelab._pg_layout_autofix(spec, [msg]), "c")["x"] == [0, 12]


def test_layout_autofix_ignores_what_it_cant_fix():
    msg = "c1: shapes 'h0' and 'h1' overlap by 20 px; on this chart 1 x unit = 41.6 px, so put their centres at least 3.46 x units apart (now 2.4), or make them smaller (w/h/r)"
    assert codelab._pg_layout_autofix(copy.deepcopy(GINI), [msg]) is None


@needs_node
def test_a_layout_code_fix_needs_no_haiku_call(monkeypatch):
    assert any("drawn over the heatmap" in m for m in codelab.check_playground(with_charts(HEAT))["layout"])
    result, _, _, sent, _ = run(monkeypatch, [with_charts(HEAT)])
    assert [s["step"] for s in sent] == ["playground write"]
    (w,) = widgets(result)
    assert "Weights (rows: query)" in w["html"] and "yLabel" not in w["html"]


@needs_node
def test_haiku_gets_the_code_fixed_spec_for_what_is_left(monkeypatch):
    both = with_charts(HEAT, CAP10)
    result, _, _, sent, _ = run(monkeypatch, [both, both])  # Haiku's layout reply is no better: the code fix stays
    assert [s["step"] for s in sent] == ["playground write", "playground layout"]
    asked = sent[1]["user"]
    assert "yLabel" not in by_id(asked["yourPreviousSpec"], "hm")
    assert any("cap: a line mark sits on the top edge" in p for p in asked["problems"][1:])
    (w,) = widgets(result)
    assert "Weights (rows: query)" in w["html"]


@needs_node
def test_a_layout_code_fix_that_breaks_a_check_is_dropped(monkeypatch):
    far = with_charts(FAR)
    assert any("never fills" in m for m in codelab.check_playground(far)["layout"])
    narrowed = codelab._pg_layout_autofix(far, codelab.check_playground(far)["layout"])
    assert codelab.check_playground(narrowed)["problems"]  # the draggable point is now outside x
    _, _, _, sent, _ = run(monkeypatch, [far, far])
    assert sent[1]["step"] == "playground layout" and by_id(sent[1]["user"]["yourPreviousSpec"], "far")["x"] == [0, 200]


# ---------- the screenshot check (_pg_look): one repair, kept only if it looks better ----------

FRAME = [{"label": "start (the reader's first view)", "png": "iVBORw0KGgo="}]


def fake_look(monkeypatch, verdicts: list) -> list[dict]:
    """Screenshots always come back; each look_review call returns the next verdict (a list of problems, or None)."""
    calls: list[dict] = []
    queue = list(verdicts)
    monkeypatch.setattr(codelab, "PG_LOOK", True)  # off by default; these tests turn it on
    monkeypatch.setattr(codelab, "snap_widget", lambda html: FRAME)

    def look_review(client, usage, frames, plan, title, checked=None, step="code lab look"):
        calls.append({"title": title, "checked": checked, "step": step})
        return queue.pop(0)

    monkeypatch.setattr(codelab, "look_review", look_review)
    return calls


@needs_node
def test_a_playground_that_looks_fine_gets_no_repair(monkeypatch):
    looks = fake_look(monkeypatch, [[]])
    result, _, _, sent, _ = run(monkeypatch, [GINI])
    assert [s["step"] for s in sent] == ["playground write"]
    assert len(looks) == 1 and looks[0]["step"] == "playground look" and "overlapping" in looks[0]["checked"]
    assert len(widgets(result)) == 1


@needs_node
def test_a_look_repair_that_looks_better_is_used(monkeypatch):
    looks = fake_look(monkeypatch, [["Frame 1: the title is cut off"], []])
    better = {**GINI, "title": "Gini Impurity (fixed)"}
    result, _, notes, sent, _ = run(monkeypatch, [GINI, better])
    assert [s["step"] for s in sent] == ["playground write", "playground look repair"]
    problems = sent[1]["user"]["problems"]
    assert problems[0] == codelab.PG_LOOK_LEAD and problems[1] == "How it looks (from screenshots): Frame 1: the title is cut off"
    assert len(looks) == 2
    (w,) = widgets(result)
    assert w["title"] == "Gini Impurity (fixed)" and any("so it was used" in n for n in notes)


@needs_node
def test_a_look_repair_that_looks_no_better_keeps_the_original(monkeypatch):
    fake_look(monkeypatch, [["Frame 1: a label overlaps"], ["Frame 1: a label overlaps"]])
    result, _, notes, _, _ = run(monkeypatch, [GINI, {**GINI, "title": "Changed"}])
    (w,) = widgets(result)
    assert w["title"] == "Gini Impurity" and any("didn't look better" in n for n in notes)


@needs_node
def test_a_look_repair_that_changes_the_controls_or_fails_a_check_keeps_the_original(monkeypatch):
    renamed = copy.deepcopy(GINI)
    for c in renamed["components"]:
        if c.get("type") == "slider":
            c["label"] = "Something else"
    for repair, reason in ((renamed, "it changed the controls"), (BROKEN, "it failed a check")):
        looks = fake_look(monkeypatch, [["Frame 1: a label overlaps"]])
        result, _, notes, _, _ = run(monkeypatch, [GINI, repair])
        (w,) = widgets(result)
        assert w["title"] == "Gini Impurity" and any(reason in n for n in notes)
        assert len(looks) == 1  # no second look for a repair that was already turned down


@needs_node
def test_the_screenshot_check_is_off_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(codelab, "snap_widget", lambda html: called.append(1) or FRAME)
    result, _, _, sent, _ = run(monkeypatch, [GINI])
    assert not called and [s["step"] for s in sent] == ["playground write"] and len(widgets(result)) == 1


@needs_node
def test_no_screenshots_means_no_judge_and_the_playground_is_kept(monkeypatch):
    called = []
    monkeypatch.setattr(codelab, "PG_LOOK", True)
    monkeypatch.setattr(codelab, "look_review", lambda *a, **k: called.append(1) or [])
    result, _, _, sent, _ = run(monkeypatch, [GINI])  # snap_widget returns None (no_screenshots)
    assert not called and [s["step"] for s in sent] == ["playground write"] and len(widgets(result)) == 1
