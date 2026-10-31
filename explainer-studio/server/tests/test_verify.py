"""Quality 2.0's verify step (verify.py): the code checks on planted problems, and the Jev/Haiku checks with both faked."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from explainer import verify as V
from explainer.core.validate import assemble_spec
from explainer.pricing import UsageLog

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample.json").read_text(encoding="utf-8"))


def spec_of() -> dict:
    return assemble_spec(SAMPLE["outline"], SAMPLE["design"], "Outline\n\nSource material:\nReader's request: transformers", [])


def checks_of(findings: list[dict]) -> list[str]:
    return [f["check"] for f in findings]


def test_the_sample_page_is_clean():
    assert V.code_checks(spec_of()) == []


def test_code_checks_find_planted_problems():
    spec = spec_of()
    st = spec["stages"][0]
    st["blocks"] = [
        *st["blocks"],
        {"type": "slider", "var": "unused_knob", "label": "Knob", "min": 0, "max": 1, "step": 0.1, "value": 0.5},
        {"type": "plot", "title": "Flat", "xLabel": "x", "yLabel": "y", "xMin": 0, "xMax": 1, "yMin": 0, "yMax": 0,
         "series": [{"label": "c", "expr": "3"}], "markerX": ""},
        {"type": "quiz", "questions": [{"q": "Pick", "choices": ["A", "a", "B"], "answer": 7, "explain": "."}]},
    ]
    spec["stages"][1]["blocks"] = [{"type": "text", "md": " ".join(["word"] * (V.MAX_WORDS + 1))}]
    spec["stages"][2]["blocks"][0] = {**spec["stages"][2]["blocks"][0]}
    spec["stages"][2]["blocks"].append({"type": "slider", "var": "temp2", "label": "Temperature $T$", "min": 0, "max": 2, "step": 0.1, "value": 1})
    found = V.code_checks(spec)
    details = " | ".join(f["detail"] for f in found)
    assert "unused_knob" in details and "changes nothing" in details
    assert "flat line" in details
    assert "repeats a choice" in details and "marks no valid answer" in details
    assert "only text" in details and "words of text" in details
    assert "raw $...$" in details
    assert {f["stage"] for f in found if "unused_knob" in f["detail"]} == {st["id"]}


def test_build_warnings_become_findings_but_run_notes_do_not():
    spec = spec_of()
    label = spec["stages"][1]["label"]
    spec["warnings"] = [
        f'Dropped an invalid "plot" block in "{label}".',
        'Readout "Share of context" goes outside the valid range for a probability.',
        "Clarity (Intro): added a paragraph",
        "Fact check (Intro): \"a\" → \"b\"",
        "The plan's JSON had a syntax slip and was repaired automatically.",
    ]
    found = [f for f in V.code_checks(spec) if f["check"] == "build"]
    assert len(found) == 2
    assert found[0]["stage"] == spec["stages"][1]["id"] and found[1]["stage"] == ""


def test_a_slider_that_barely_moves_anything_is_weak():
    spec = spec_of()
    st = spec["stages"][0]
    st["blocks"] = [
        *st["blocks"],
        {"type": "slider", "var": "tiny", "label": "Tiny knob", "min": 0, "max": 1, "step": 0.1, "value": 0.5},
        {"type": "readout", "defs": [], "items": [{"label": "Out", "expr": "100 + tiny * 0.01", "digits": 2}]},
        {"type": "slider", "var": "big", "label": "Big knob", "min": 1, "max": 5, "step": 1, "value": 2},
        {"type": "readout", "defs": [], "items": [{"label": "Out2", "expr": "big * 3", "digits": 2}]},
    ]
    details = " | ".join(f["detail"] for f in V.code_checks(spec))
    assert "Tiny knob" in details and "barely moves" in details
    assert "Big knob" not in details


def test_later_rounds_check_errors_only(monkeypatch):
    ran = []
    monkeypatch.setattr(V, "code_checks", lambda spec: [{"check": "length", "stage": "", "detail": "long"}, {"check": "build", "stage": "", "detail": "dropped"}])
    monkeypatch.setattr(V, "fact_checks", lambda *a: ran.append("facts") or [])
    monkeypatch.setattr(V, "clarity_checks", lambda *a: ran.append("clarity") or [])
    monkeypatch.setattr(V, "coverage_checks", lambda *a: ran.append("coverage") or [])
    monkeypatch.setattr(V, "style_checks", lambda jev, spec, targets, usage, kinds=None: ran.append(("style", kinds)) or [])
    r = V.verify(None, "jev", spec_of(), "x", UsageLog("quality-deep"), errors_only=True)
    assert [f["check"] for f in r["findings"]] == ["build"]
    assert sorted(map(str, ran)) == sorted(["facts", str(("style", V.ERROR_STYLE))])


def test_charts_get_the_plot_checks():
    """A number chart is checked as the line it draws over its x slider: flat, and weak sliders through it."""
    spec = spec_of()
    st = spec["stages"][0]
    chart = {"type": "chart", "title": "C", "lesson": "l", "defs": [], "x": "knob", "by": "", "at": [], "view": "line",
             "labels": [], "caption": "", "series": [{"label": "c", "expr": "3"}]}
    st["blocks"] = [
        *st["blocks"],
        {"type": "slider", "var": "knob", "label": "Knob", "min": 0, "max": 1, "step": 0.1, "value": 0.5},
        chart,
        {"type": "slider", "var": "tiny", "label": "Tiny knob", "min": 0, "max": 1, "step": 0.1, "value": 0.5},
        {**chart, "title": "Weak", "series": [{"label": "w", "expr": "100 + tiny * 0.01 + knob"}]},
    ]
    details = " | ".join(f["detail"] for f in V.code_checks(spec))
    assert 'The chart "C" is a flat line' in details
    assert "Tiny knob" in details and "barely moves" in details
    # Random charts have no line to judge.
    assert V._chart_plot({**chart, "view": "histogram"}, {"knob": {"min": 0, "max": 1}}) is None


def test_a_plot_that_moves_is_not_flat():
    plot = {"type": "plot", "xMin": 0, "xMax": 1, "series": [{"label": "a", "expr": "3"}, {"label": "b", "expr": "x * 2"}]}
    assert not V._flat(plot, {})
    assert V._flat({**plot, "series": plot["series"][:1]}, {})


class FakeJev:
    """Answers every question: nouls with p, scores with norm, so each check can be pushed either way."""

    def __init__(self, p: float = 0.9, norm: float = 0.0):
        self.p, self.norm, self.calls = p, norm, []

    def __call__(self, key, state, questions):
        self.calls.append((state, questions))
        answers = {q: ({"type": "noul", "p": self.p} if d["type"] == "noul" else {"type": "score", "norm": self.norm, "confidence": 0.9, "score": 0})
                   for q, d in questions.items()}
        return {"answers": answers, "model": "jev", "usage": {"input_tokens": 1}}


def fake_haiku(client, usage, step, *, system, user, max_tokens, fmt=None, **kw):
    if step == "deepen gaps":
        return json.dumps({"gaps": [{"topic": "Positional encoding", "why": "order matters"}]})
    return json.dumps({"stages": []})  # clarity topics: no ideas listed


def test_verify_reads_only_and_reports_every_check(monkeypatch):
    jev = FakeJev(p=0.1, norm=1.0)  # everything looks wrong to Jev
    for mod in ("explainer.verify", "explainer.clarity"):
        monkeypatch.setattr(f"{mod}.jev_ask", jev)
    monkeypatch.setattr("explainer.verify.write_text", fake_haiku)
    monkeypatch.setattr("explainer.clarity.write_text", fake_haiku)
    monkeypatch.setattr("explainer.verify.score", lambda key, spec, claims, values, usage: {c["id"]: 0.2 for c in claims[:2]})
    spec = spec_of()
    before = copy.deepcopy(spec)
    v = V.verify(None, "key", spec, "How do transformers work?", UsageLog("quality-deep"))
    assert spec == before
    assert {"facts", "style", "coverage", "clarity"} <= set(checks_of(v["findings"])) and v["skipped"] == []
    assert [f["id"] for f in v["findings"]] == [f"f{i + 1}" for i in range(len(v["findings"]))]
    assert any("Positional encoding" in f["detail"] for f in v["findings"] if f["check"] == "coverage")


def test_verify_only_checks_the_named_stages_closely(monkeypatch):
    jev = FakeJev(p=0.9, norm=1.0)
    monkeypatch.setattr("explainer.verify.jev_ask", jev)
    monkeypatch.setattr("explainer.clarity.jev_ask", jev)
    monkeypatch.setattr("explainer.verify.write_text", fake_haiku)
    monkeypatch.setattr("explainer.clarity.write_text", fake_haiku)
    seen = {}
    monkeypatch.setattr("explainer.verify.score", lambda key, spec, claims, values, usage: seen.setdefault("stages", {c["stage"] for c in claims}) and {})
    spec = spec_of()
    v = V.verify(None, "key", spec, "x", UsageLog("quality-deep"), only=[2])
    assert v["stages"] == 1 and seen["stages"] == {2}
    style = {f["stage"] for f in v["findings"] if f["check"] == "style"}
    assert style == {spec["stages"][2]["id"]}


def test_a_failing_check_is_skipped_not_fatal(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("Jev overloaded")
    monkeypatch.setattr("explainer.verify.jev_ask", broken)
    monkeypatch.setattr("explainer.clarity.jev_ask", broken)
    monkeypatch.setattr("explainer.verify.write_text", fake_haiku)
    monkeypatch.setattr("explainer.clarity.write_text", fake_haiku)
    monkeypatch.setattr("explainer.verify.score", broken)
    v = V.verify(None, "key", spec_of(), "x", UsageLog("quality-deep"))
    assert set(v["skipped"]) == {"facts", "style", "coverage", "clarity"}
    assert all(f["check"] != "facts" for f in v["findings"])  # the code checks still ran
