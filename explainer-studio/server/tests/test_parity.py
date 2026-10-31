"""The Python port must give the same results as the TypeScript it was ported from.

scripts/parity.mjs runs the TypeScript (shared/*.ts) over a corpus and saves
inputs and outputs to tests/parity/*.json (`npm run parity` regenerates it and
runs these tests). Numbers may differ in the last bits (libm vs V8); text,
warnings, structure and key sets must match exactly.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from explainer.core import compact, examples, expr, factcheck, lab, references, spec, validate
from explainer.js import js_str, to_fixed, to_precision

CORPUS = Path(__file__).parent / "parity"
pytestmark = pytest.mark.skipif(not (CORPUS / "outlines.json").exists(), reason="no parity corpus: run `npm run parity`")


def load(name: str) -> Any:
    return json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def norm(v: Any) -> Any:
    """Python values in the corpus's encoding: non-finite numbers and JS undefined as tagged strings."""
    if v is None:
        return "__undefined"
    if isinstance(v, bool) or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        if v != v:
            return "__num:NaN"
        if math.isinf(v):
            return "__num:Infinity" if v > 0 else "__num:-Infinity"
        return v
    if isinstance(v, list):
        return [norm(x) for x in v]
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    raise TypeError(type(v))


def same(a: Any, b: Any, path: str = "") -> None:
    """Deep equality with a relative tolerance on numbers (1 ulp-level differences between libms are fine)."""
    if isinstance(a, bool) or isinstance(b, bool):
        assert a is b, f"{path}: {a!r} != {b!r}"
        return
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), f"{path}: {a!r} != {b!r}"
        return
    if isinstance(a, dict) and isinstance(b, dict):
        assert set(a) == set(b), f"{path}: keys {sorted(set(a) ^ set(b))} differ"
        for k in a:
            same(a[k], b[k], f"{path}.{k}")
        return
    if isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{path}: length {len(a)} != {len(b)}\n  py: {json.dumps(a)[:600]}\n  ts: {json.dumps(b)[:600]}"
        for i, (x, y) in enumerate(zip(a, b)):
            same(x, y, f"{path}[{i}]")
        return
    assert a == b, f"{path}: {a!r} != {b!r}"


def jsonish(v: Any) -> Any:
    """Undefined-free JSON shape (what JSON.stringify keeps), for comparing specs and blocks."""
    return json.loads(json.dumps(v, allow_nan=True)) if v is not None else None


# ---------- JS number formatting ----------


def test_number_formatting():
    for c in load("numbers"):
        n = c["n"]
        assert js_str(n) == c["str"], n
        assert [to_fixed(n, d) for d in (0, 1, 2, 3, 4, 6)] == c["fixed"], n
        assert [to_precision(n, p) for p in (1, 2, 3, 4, 6)] == c["precision"], n


# ---------- the math language ----------


def test_expressions():
    data = load("expressions")
    defs = [{"name": "a", "expr": "normal(0, 1)"}]
    for c in data["cases"]:
        src = c["src"].replace("NaN_", "NaN_")
        r = expr.try_compile(src)
        if c["compile"]["ok"]:
            assert r["ok"], (src, r.get("error"))
            assert list(r["c"].vars) == c["compile"]["vars"], src
            assert r["c"].random == c["compile"]["random"], src
        else:
            assert not r["ok"] and r["error"] == c["compile"]["error"], (src, r)
        for scope, want in zip(data["scopes"], c["values"]):
            same(norm(expr.eval_value(src, dict(scope))), want, src)
        assert expr.uses_random(src, defs) == c["usesRandom"], src


def test_requested_stages():
    for c in load("stages"):
        assert validate.requested_stages(c["text"]) == c["result"], c["text"]


def test_references():
    data = load("references")
    for p in data["papers"]:
        r = references.cut_references(p["sections"])
        same(r, p["result"])
    for c in data["lines"]:
        assert references.is_reference_line(c["line"]) == c["result"], c["line"]


# ---------- outlines: parse, then assemble ----------


def test_outlines():
    for c in load("outlines"):
        parsed = spec.parse_outline(c["raw"])
        same(norm(parsed), c["parsed"], f"{c['name']} parse")
        if "spec" in c:
            s = validate.assemble_spec(parsed["outline"], c["design"], "Context for the chatbot.", parsed["warnings"])
            same(norm(jsonish(s)), c["spec"], f"{c['name']} spec")


def _specs() -> dict[str, dict]:
    return {c["name"]: c["spec"] for c in load("outlines") if "spec" in c}


def test_specs():
    specs = _specs()
    for c in load("specs"):
        s = specs[c["name"]]
        name = c["name"]
        claims = factcheck.claims_of(s)
        same(claims, c["claims"], f"{name} claims")
        assert factcheck.page_values(s) == c["pageValues"], name
        same(examples.example_briefs(s), c["exampleBriefs"], f"{name} example briefs")
        r = factcheck.apply_fixes(s, claims, c["fixes"])
        changed = [{"i": i, "blocks": st["blocks"]} for i, st in enumerate(r["spec"]["stages"]) if jsonish(st) != jsonish(s["stages"][i])]
        same({"applied": r["applied"], "skipped": r["skipped"], "changed": jsonish(changed)}, c["applied"], f"{name} fixes")
        for e in c["edits"]:
            got = validate.check_edit(s, e["stage"], e["blocks"])
            if got["ok"]:
                got = {"ok": True, "notes": got["notes"], "stageBlocks": jsonish(got["spec"]["stages"][e["stage"]]["blocks"])}
            same(got, e["result"], f"{name} edit")
        briefs = examples.example_briefs(s)
        stepped = examples.apply_example_steps(s, {b["key"]: ["one", "two", "three", "four", "five", "six", "seven"] for b in briefs})
        same([b for st in stepped["stages"] for b in st["blocks"] if b["type"] == "examples"], c["stepped"], f"{name} steps")


# ---------- compact format and Freeform+ briefs ----------


def test_compact():
    for c in load("compact"):
        if "error" in c:
            with pytest.raises(ValueError) as e:
                compact.parse_compact(c["text"])
            assert str(e.value) == c["error"]
        else:
            same(norm(compact.parse_compact(c["text"])), c["result"], c["text"][:80])


def test_lab():
    for c in load("lab"):
        parsed = lab.parse_lab_outline(c["raw"])
        same(norm(parsed), c["parsed"], "lab parse")
        if "outline" in parsed:
            same(lab.briefs_of(parsed["outline"]), c["briefs"], "briefs")
            same(lab.merge_bundles(parsed["outline"], dict(c["bundles"])), c["merged"], "merge")
