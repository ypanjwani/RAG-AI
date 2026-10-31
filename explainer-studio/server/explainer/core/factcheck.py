"""Port of shared/factcheck.ts: the checkable statements of a finished explainer, the numbers
its code computes, and how to apply a reviewer's corrections. A fix may only replace text
inside the claim's own fields (or change a quiz's marked answer)."""

from __future__ import annotations

import re
from typing import Any

from ..js import WS, is_finite, is_integer, js_len, js_slice, js_str, js_trim, json_stringify, round_num
from .edit import apply_changes, get_at, key_of, leaves
from .expr import eval_number
from .views import with_defs

MAX_CLAIMS = 120

# Fields that state things. Labels, titles of charts, axis names and ids don't.
PROSE = {"md", "back", "meaning", "caption", "evidence", "breaksIf", "detail", "desc", "points"}
# Sentences that only tell the reader what to do with the page.
NAVIGATION = re.compile(r"^(try|move|drag|click|press|use the|set the|change the|turn|flip|toggle|scroll|look at|see the|watch)\b", re.I | re.A)
_BULLET = re.compile(r"^" + WS + r"*(?:[-*]|\d+\.)" + WS + r"+", re.A)
_SPLIT = re.compile(r"(?<=[.!?])" + WS + r"+(?=[A-Z*$(\"'\[=])", re.A)


def sentences(md: str) -> list[str]:
    """Split Markdown prose into sentences (and list items), keeping each as written so it can be found again."""
    out: list[str] = []
    for line in re.split(r"\n+", md):
        for s in _SPLIT.split(_BULLET.sub("", line, count=1)):
            s = js_trim(s)
            if js_len(s) >= 25 and re.search(r"[a-z]", s, re.I | re.A) and not NAVIGATION.search(re.sub(r"^\*+", "", s)):
                out.append(s)
    return out


def _letter(i: Any) -> str:
    return chr(65 + int(i))


def _question_claim(q: dict, base: list) -> dict:
    q_key = "q" if "q" in q else "question"
    answer = q["answer"]
    marked = q["choices"][int(answer)] if 0 <= answer < len(q["choices"]) and is_integer(answer) else "?"
    choices = "  ".join(f"{_letter(i)}) {c}" for i, c in enumerate(q["choices"]))
    return {
        "text": f"Question: {q.get('q', q.get('question'))}\nChoices: {choices}\nMarked correct: {_letter(answer)}) {marked}\nExplanation: {q['explain']}",
        "fields": [[*base, q_key], *[[*base, "choices", i] for i in range(len(q["choices"]))], [*base, "explain"]],
        "question": base,
    }


def _block_claims(b: dict) -> list[dict]:
    t = b["type"]
    if t == "quiz":
        return [_question_claim(q, ["questions", i]) for i, q in enumerate(b["questions"])]
    if t == "predict":
        return [_question_claim(b, [])]
    if t == "cards":
        # A card is checked as a whole: its figure, title, text and evidence belong together.
        out = []
        for i, it in enumerate(b["items"]):
            keys = [k for k in ("value", "title", "md", "evidence", "breaksIf") if it.get(k)]
            if b["variant"] == "mistake":
                # A mistake card's title is a misconception on purpose; only its correction must be true.
                text = f'Misconception (shown on purpose, to be corrected): "{it["title"]}". Correction: {it["md"]}'
            else:
                text = " — ".join(it[k] if k in ("md", "title") else f"{k}: {it[k]}" for k in keys)
            fields = [["items", i, k] for k in keys if b["variant"] != "mistake" or k != "title"]
            if js_len(text) >= 25:
                out.append({"text": text, "fields": fields})
        return out
    if t == "table":
        out = []
        for ri, r in enumerate(b["rows"]):
            cells = [(c, ci) for ci, c in enumerate(r) if c and not c.startswith("=")]
            if not cells:
                continue
            text = " | ".join(f"{(b['columns'][ci] if ci < len(b['columns']) else '') or '—'}: {c}" for c, ci in cells)
            if js_len(text) >= 25:
                out.append({"text": f"{b['title']}: {text}", "fields": [["rows", ri, ci] for _, ci in cells]})
        return out
    # Widgets are sandboxed code; a worked example's numbers are computed at its own settings, not the page's.
    if t in ("widget", "examples"):
        return []
    out = []
    for leaf in leaves(b):
        if leaf["kind"] in ("prose", "text") and isinstance(leaf["value"], str) and key_of(leaf["path"]) in PROSE:
            heading = _heading_of(b, leaf["path"])
            out.extend({"text": f"{heading}: {s}" if heading else s, "fields": [leaf["path"]]} for s in sentences(leaf["value"]))
    return out


def _heading_of(b: dict, path: list) -> str:
    """The heading a nested sentence sits under (see headingOf in factcheck.ts); the block's own title is not used."""
    for n in range(len(path) - 1, 0, -1):
        o = get_at(b, path[:n])
        if isinstance(o, dict):
            for k in ("title", "label"):
                t = o.get(k)
                if isinstance(t, str) and js_trim(t):
                    return js_trim(t)
    return ""


def claims_of(spec: dict) -> list[dict]:
    """Every checkable statement on the page, capped at MAX_CLAIMS (earlier stages first)."""
    out: list[dict] = []
    for si, st in enumerate(spec["stages"]):
        for bi, b in enumerate(st["blocks"]):
            for c in _block_claims(b):
                out.append({**c, "id": f"c{len(out) + 1}", "stage": si, "block": bi})
    return out[:MAX_CLAIMS]


def _default_scope(spec: dict) -> dict:
    scope: dict = {"seed": 0.0}
    for st in spec["stages"]:
        for b in st["blocks"]:
            if b["type"] in ("slider", "choice", "toggle") and b["var"] not in scope:
                scope[b["var"]] = b["value"]
    return scope


def _show(v: float) -> str:
    return js_str(round_num(v, 4))


def page_values(spec: dict, max_lines: int = 60) -> list[str]:
    """The numbers the page shows when it opens, computed by code, as lines like `readout "Recall" = 0.6914`."""
    scope = _default_scope(spec)
    out: list[str] = []

    def val(expr: str, defs: list | None = None) -> float:
        return eval_number(expr, with_defs(defs or [], scope))

    for st in spec["stages"]:
        for b in st["blocks"]:
            t = b["type"]
            if t in ("slider", "toggle", "choice"):
                out.append(f'control "{b["label"]}" ({b["var"]}) starts at {js_str(b["value"])}')
            if t == "readout":
                for it in b["items"]:
                    v = val(it["expr"], b.get("defs"))
                    if is_finite(v):
                        extra = f" (shown as {_show(v * 100)}%)" if it.get("kind") == "probability" else ""
                        out.append(f'readout "{it["label"]}" = {_show(v)}{extra}')
            if t == "calculator":
                for i, r in enumerate(b["rows"]):
                    v = val(r["expr"], b.get("defs"))
                    if is_finite(v):
                        note = f", {r['note']}" if r["note"] else ""
                        out.append(f'calculator "{b["title"]}" row {i + 1} ({r["latex"]}{note}) = {_show(v)}')
            if t == "table":
                for r in b["rows"]:
                    for ci, c in enumerate(r):
                        if not c.startswith("="):
                            continue
                        v = val(c[1:])
                        if is_finite(v):
                            col = b["columns"][ci] if ci < len(b["columns"]) else "undefined"
                            out.append(f'table "{b["title"]}", row "{r[0]}", column "{col}" = {_show(v)}')
    return out[:max_lines]


def js_replace_first(s: str, find: str, rep: str) -> str:
    """s.replace(find, rep) with a string pattern: the first occurrence, and JS's $ patterns in rep ($$, $&, $`, $')."""
    i = s.find(find)
    if i < 0:
        return s
    before, after = s[:i], s[i + len(find):]
    out: list[str] = []
    j = 0
    while j < len(rep):
        c = rep[j]
        if c == "$" and j + 1 < len(rep):
            n = rep[j + 1]
            if n == "$":
                out.append("$")
                j += 2
                continue
            if n == "&":
                out.append(find)
                j += 2
                continue
            if n == "`":
                out.append(before)
                j += 2
                continue
            if n == "'":
                out.append(after)
                j += 2
                continue
        out.append(c)
        j += 1
    return before + "".join(out) + after


def apply_fixes(spec: dict, claims: list[dict], fixes: list[dict]) -> dict:
    """Apply fixes to a copy of the spec; a fix that doesn't apply cleanly is skipped whole. {"spec", "applied", "skipped"}."""
    by_id = {c["id"]: c for c in claims}
    stages = [{**s, "blocks": list(s["blocks"])} for s in spec["stages"]]
    applied: list[dict] = []
    skipped: list[dict] = []

    for fix in fixes:
        c = by_id.get(fix["id"])
        if c is None:
            skipped.append({"id": fix["id"], "why": "unknown claim"})
            continue
        block = stages[c["stage"]]["blocks"][c["block"]]
        changes: list[dict] = []
        notes: list[dict] = []
        why = ""

        def current(p: list) -> str:
            v = get_at(block, p)
            key = json_stringify(p)
            for ch in changes:
                if json_stringify(ch["path"]) == key:
                    v = ch["value"]
            return v

        for e in fix.get("edits", []):
            if not e["find"] or e["find"] == e["replace"]:
                continue
            hits = [p for p in c["fields"] if isinstance(get_at(block, p), str) and current(p).count(e["find"]) == 1]
            if len(hits) != 1:
                why = f'"{js_slice(e["find"], 0, 60)}" ' + ("is ambiguous" if hits else "isn't in the claim")
                break
            changes.append({"path": hits[0], "value": js_replace_first(current(hits[0]), e["find"], e["replace"])})
            notes.append({"from": e["find"], "to": e["replace"]})
        answer = fix.get("answer")
        if not why and answer is not None:
            q = get_at(block, c["question"]) if c.get("question") is not None else None
            if not q or not is_integer(answer) or answer < 0 or answer >= len(q["choices"]):
                why = "invalid answer"
            elif answer != q["answer"]:
                changes.append({"path": [*c["question"], "answer"], "value": answer})
                notes.append({"from": f"marked answer {_letter(q['answer'])}", "to": f"marked answer {_letter(answer)}"})
        if why:
            skipped.append({"id": fix["id"], "why": why})
            continue
        if not changes:
            continue
        r = apply_changes(block, changes)
        if "error" in r:
            skipped.append({"id": fix["id"], "why": r["error"]})
            continue
        stages[c["stage"]]["blocks"][c["block"]] = r["block"]
        for n in notes:
            applied.append({"id": fix["id"], "where": stages[c["stage"]]["label"], **n})
    return {"spec": {**spec, "stages": stages}, "applied": applied, "skipped": skipped}
