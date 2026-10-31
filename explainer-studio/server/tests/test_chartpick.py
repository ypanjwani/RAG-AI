"""The chart switch and chartpick.experiments_to_charts: numeric experiments become charts (Jev faked)."""

from __future__ import annotations

from explainer import chartpick as C
from explainer import pipeline
from explainer.core.validate import assemble_spec
from explainer.pricing import UsageLog

SLIDER = {"type": "slider", "var": "B", "label": "Number of models B", "min": 1, "max": 100, "step": 1, "value": 25, "unit": ""}


def experiment(**over) -> dict:
    return {"type": "experiment", "title": "Run the crowd", "valueLabel": "average error of B models", "repeats": [1, 10, 100],
            "trial": "mean(normal(0, 1, B))", "caption": "Watch the results cluster tightly when B is large and scatter when B is small.", **over}


def outline(*blocks: dict) -> dict:
    return {"title": "Bagging", "subtitle": "", "level": "student", "layout": "story", "keyIntuition": "k",
            "phases": [{"id": "p", "label": "P"}], "glossary": [],
            "stages": [{"id": "s", "label": "S", "phase": "p", "blurb": "", "blocks": [{"type": "text", "md": "x"}, SLIDER, *blocks]}]}


def blocks(o: dict) -> list[dict]:
    return o["stages"][0]["blocks"][2:]


def test_without_jev_a_numeric_experiment_on_a_setting_becomes_a_compare_chart():
    warnings: list[str] = []
    out = C.experiments_to_charts(outline(experiment()), None, UsageLog("quality-plus"), warnings)
    [chart] = blocks(out)
    assert chart["type"] == "chart" and chart["view"] == "compare" and chart["by"] == "B"
    assert chart["series"] == [{"label": "average error of B models", "expr": "mean(normal(0, 1, B))"}]
    assert warnings == ['Chart switch: the experiment "Run the crowd" is shown as a compare chart of Number of models B.']
    # It assembles cleanly: compare at the slider's two ends.
    spec = assemble_spec(out, None, "")
    got = [b for b in spec["stages"][0]["blocks"] if b["type"] == "chart"][0]
    assert got["at"] == [1, 100]
    assert not [w for w in spec["warnings"] if "Chart" in w or "chart" in w]


def test_an_experiment_that_reads_no_setting_becomes_a_running_chart():
    out = C.experiments_to_charts(outline(experiment(trial="mean(normal(0, 1, 25))")), None, UsageLog("quality-plus"), [])
    assert blocks(out)[0]["view"] == "running" and blocks(out)[0]["by"] == ""


def test_experiments_with_named_outcomes_stay():
    e = experiment(trial="bernoulli(0.5)", outcomes=["heads", "tails"])
    out = C.experiments_to_charts(outline(e), None, UsageLog("quality-plus"), [])
    assert blocks(out) == [e]


def test_an_experiment_a_chart_already_shows_is_dropped():
    chart = {"type": "chart", "title": "Spread", "lesson": "l", "series": [{"label": "m", "expr": "mean(normal(0, 1, B))"}],
             "x": "", "by": "B", "at": [], "view": "compare", "labels": [], "caption": ""}
    warnings: list[str] = []
    out = C.experiments_to_charts(outline(chart, experiment()), None, UsageLog("quality-plus"), warnings)
    assert blocks(out) == [chart]
    assert warnings[0].startswith('Chart switch: dropped the experiment "Run the crowd"')


def test_jev_picks_the_view_when_confident(monkeypatch):
    def fake(key, state, questions, p=0.9):
        assert set(questions["view"]["criteria"]) == {"histogram", "running", "compare"}
        assert state["experiment"]["setting"] == "Number of models B"
        return {"answers": {"view": {"ranking": [{"option": "histogram", "p": p}]}}, "model": "jev-1", "usage": {"input_tokens": 50}}

    monkeypatch.setattr(C, "jev_ask", fake)
    usage = UsageLog("quality-plus")
    assert blocks(C.experiments_to_charts(outline(experiment()), "key", usage, []))[0]["view"] == "histogram"
    # An unsure answer falls back to the fixed rule.
    monkeypatch.setattr(C, "jev_ask", lambda k, s, q: fake(k, s, q, p=0.3))
    assert blocks(C.experiments_to_charts(outline(experiment()), "key", usage, []))[0]["view"] == "compare"


def test_jev_errors_fall_back_to_the_rule(monkeypatch):
    def broken(*a):
        raise C.JevError("down")

    monkeypatch.setattr(C, "jev_ask", broken)
    assert blocks(C.experiments_to_charts(outline(experiment()), "key", UsageLog("quality-plus"), []))[0]["view"] == "compare"


def test_the_switch_is_on_by_default_and_picks_the_prompt(monkeypatch):
    monkeypatch.delenv("CHART_BLOCK", raising=False)
    assert C.chart_on()
    assert "- chart: use it for EVERY graph" in pipeline.planner_system()
    monkeypatch.setenv("CHART_BLOCK", "0")
    assert not C.chart_on()
    assert "- chart: use it" not in pipeline.planner_system()
