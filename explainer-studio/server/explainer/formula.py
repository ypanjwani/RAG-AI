""""Go deeper" on a formula block (POST /api/formula): when a reader clicks the button next to a formula, one Haiku
call explains every term, where the formula comes from, why it looks like this, a tiny worked example and a common
misreading (FORMULA_DEEPER_SYSTEM, format formulaDeeper). Nothing runs until the click; the app saves the note on the
page (spec.formulaNotes), so reopening it and exports cost nothing."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from . import data
from .js import js_len, js_trim
from .json_util import extract_json
from .llm import ReaderError, write_text
from .pricing import UsageLog

MAX_TERMS = 16  # legend rows sent, and terms kept from the reply
MAX_STEPS = 6  # origin and example steps kept
FIELD_CHARS = 1500  # any one text field kept from the reply


def parse_formula(body: Any) -> dict | str:
    """A /api/formula body, validated; a string is the error to show."""
    body = body if isinstance(body, dict) else {}
    limits = data.limits()
    latex = js_trim(body["latex"]) if isinstance(body.get("latex"), str) else ""
    if not latex:
        return "This formula is empty."
    if js_len(latex) > limits["formulaLatex"]:
        return "This formula is too long."
    text = js_trim(body["text"]) if isinstance(body.get("text"), str) else ""
    if js_len(text) > limits["sectionChars"]:
        return "This section is too long."
    terms = body.get("terms", [])
    if not isinstance(terms, list) or not all(isinstance(t, dict) and isinstance(t.get("symbol"), str) and isinstance(t.get("meaning"), str) for t in terms):
        return "Invalid formula request."
    s = lambda k, n: body[k][:n] if isinstance(body.get(k), str) else ""  # noqa: E731
    level = body.get("level") if body.get("level") in data.menu("LEVELS") else "beginner"
    return {"title": s("title", 300), "stage": s("stage", 200), "level": level, "latex": latex, "caption": s("caption", 500),
            "terms": [{"symbol": t["symbol"][:200], "meaning": t["meaning"][:400]} for t in terms[:MAX_TERMS]], "text": text}


def _user_turn(req: dict) -> str:
    legend = "\n".join(f"- ${t['symbol']}$: {t['meaning']}" for t in req["terms"]) or "(none)"
    return (f"Explainer: {req['title']}\nReader level: {data.menu('LEVELS')[req['level']]}\nSection: {req['stage']}\n\n"
            f"Formula (LaTeX):\n{req['latex']}\n\nCaption: {req['caption'] or '(none)'}\n\nLegend on the page:\n{legend}\n\n"
            f"Section text (context):\n{req['text'] or '(none)'}")


def _text(v: Any) -> str:
    return v.strip()[:FIELD_CHARS] if isinstance(v, str) else ""


def clean_note(reply: Any) -> dict | None:
    """The reply as a FormulaNote (types checked, lengths capped), or None if it is unusable."""
    if not isinstance(reply, dict):
        return None
    terms = []
    for t in reply.get("terms") or []:
        if isinstance(t, dict) and _text(t.get("symbol")):
            terms.append({k: _text(t.get(k)) for k in ("symbol", "name", "meaning", "role")})
    steps = lambda k: [x for x in (_text(v) for v in (reply.get(k) or []) if isinstance(reply.get(k), list)) if x][:MAX_STEPS]  # noqa: E731
    note = {"idea": _text(reply.get("idea")), "terms": terms[:MAX_TERMS], "origin": steps("origin"), "shape": _text(reply.get("shape")),
            "example": steps("example"), "pitfall": _text(reply.get("pitfall"))}
    if not note["idea"] or not note["terms"] or not note["origin"]:
        return None
    return note


def formula_turn(client: anthropic.Anthropic, req: dict) -> dict:
    """{"note", "usd"} for one click. Raises if the reply is unusable (the route turns that into a readable error)."""
    usage = UsageLog("formula", "low")
    try:
        text = write_text(client, usage, "formula deeper", system=data.prompt("FORMULA_DEEPER_SYSTEM"), user=_user_turn(req),
                          max_tokens=6000, fmt=data.formats()["formulaDeeper"], effort="low")
        try:
            reply, _ = extract_json(text)
        except (ValueError, json.JSONDecodeError):
            reply = None
        note = clean_note(reply)
        if note is None:
            raise ReaderError("The explanation came back incomplete. Please try again.")
    except Exception as e:
        e.spent_usd = usage.summary()["totalUsd"]  # type: ignore[attr-defined]  # the route records it (audit F23)
        raise
    return {"note": note, "usd": usage.summary()["totalUsd"]}
