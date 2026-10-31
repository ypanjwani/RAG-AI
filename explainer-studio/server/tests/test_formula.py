""""Go deeper" on a formula (POST /api/formula), with Claude faked."""

from __future__ import annotations

import json

import pytest

from conftest import signup
from explainer import formula
from explainer.formula import clean_note, formula_turn, parse_formula


def req(**extra) -> dict:
    return {"title": "Attention", "level": "student", "stage": "Scaled scores", "latex": r"\frac{QK^\top}{\sqrt{d_k}}",
            "caption": "Scaled dot-product scores", "terms": [{"symbol": "d_k", "meaning": "key size"}], "text": "Scores are scaled.", **extra}


NOTE = {
    "idea": "It measures how well each query matches each key, kept at a steady size.",
    "terms": [{"symbol": "Q", "name": "Queries", "meaning": "What each token looks for.", "role": "One side of the match."},
              {"symbol": r"\sqrt{d_k}", "name": "Scale", "meaning": "Root of the key size.", "role": "Keeps scores from growing with $d_k$."}],
    "origin": ["Start with a dot product $q \\cdot k$.", "Its spread grows like $\\sqrt{d_k}$.", "Divide by it."],
    "shape": "A square root, because the standard deviation of a sum of $d_k$ terms grows like $\\sqrt{d_k}$.",
    "example": ["$q \\cdot k = 8$, $d_k = 16$.", "$8 / 4 = 2$."],
    "pitfall": "The scale is not a learned weight.",
}


def test_parse_formula():
    r = parse_formula(req())
    assert r["latex"] == r"\frac{QK^\top}{\sqrt{d_k}}" and r["level"] == "student" and r["terms"] == [{"symbol": "d_k", "meaning": "key size"}]
    assert parse_formula(req(level="wizard"))["level"] == "beginner"
    assert parse_formula(req(latex="  ")) == "This formula is empty."
    assert parse_formula(req(latex="x" * 2001)) == "This formula is too long."
    assert parse_formula(req(text="x" * 8001)) == "This section is too long."
    assert parse_formula(req(terms=[{"symbol": 1}])) == "Invalid formula request."
    assert parse_formula(req(terms=[{"symbol": "a", "meaning": "b"}] * 40))["terms"] == [{"symbol": "a", "meaning": "b"}] * formula.MAX_TERMS


def test_clean_note():
    assert clean_note(NOTE) == NOTE
    assert clean_note({**NOTE, "origin": []}) is None and clean_note({**NOTE, "terms": []}) is None and clean_note("x") is None
    capped = clean_note({**NOTE, "origin": ["s"] * 10, "example": "not a list", "pitfall": 3})
    assert len(capped["origin"]) == formula.MAX_STEPS and capped["example"] == [] and capped["pitfall"] == ""


def test_formula_turn(monkeypatch):
    sent: dict = {}

    def write_text(client, usage, step, *, system, user, max_tokens, **kw):
        sent.update(system=system, user=user, fmt=kw["fmt"])
        usage.add(step, "claude-haiku-5-5", {"input_tokens": 1000, "output_tokens": 800, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        return json.dumps(NOTE)

    monkeypatch.setattr(formula, "write_text", write_text)
    r = formula_turn(None, parse_formula(req()))
    assert r["note"] == NOTE and r["usd"] > 0
    assert sent["system"] == formula.data.prompt("FORMULA_DEEPER_SYSTEM") and sent["fmt"] == formula.data.formats()["formulaDeeper"]
    assert r"\sqrt{d_k}" in sent["user"] and "$d_k$: key size" in sent["user"] and "Scores are scaled." in sent["user"]
    monkeypatch.setattr(formula, "write_text", lambda *a, **k: json.dumps({**NOTE, "origin": []}))
    with pytest.raises(ValueError, match="incomplete"):
        formula_turn(None, parse_formula(req()))


def test_formula_route(client):
    res = client.post("/api/formula", data='{"latex": ""}', headers=signup(client))
    assert res.status_code == 400 and res.get_json()["error"] == "This formula is empty."
