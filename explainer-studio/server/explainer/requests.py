"""Request validation for /api/generate and /api/chat (the same rules and messages as before the move to Flask)."""

from __future__ import annotations

from typing import Any

from . import data
from .deepen import ROUNDS_CEILING
from .followup import parse_follow_up
from .js import js_len, js_slice, js_string, js_trim


def _get(body: Any, key: str) -> Any:
    return body.get(key) if isinstance(body, dict) else None


def parse_generate(body: Any) -> dict | str:
    """A GenerateRequest, or the error to show."""
    limits = data.limits()
    # Quality+, Quality 2.0 (quality-deep, deepen.py), Lite+ and Freeform+ are offered. Retired modes in old records (quality, quality-fixed, custom,
    # lab, cheap, economy, compact-plus, and quality-2, merged into Quality+) run as Quality+, and plain
    # freeform as Freeform+.
    q = _get(body, "quality")
    quality = "freeform-plus" if q in ("freeform", "freeform-plus") else q if q in ("lite-plus", "quality-deep", "smart", "scene-plus", "code-lab", "code-lab-2", "code-lab-3", "code-lab-7", "playground") else "quality-plus"
    opts: dict = {}
    effort = _get(body, "effort")
    if effort in ("low", "medium", "high"):  # absent = server default (PLAN_EFFORT or medium)
        opts["effort"] = effort
    interactivity = _get(body, "interactivity")
    if interactivity in ("reading", "hands-on"):  # absent or "balanced" = the planner's usual mix
        opts["interactivity"] = interactivity
    level = _get(body, "level")
    if isinstance(level, str) and level != "auto" and level in data.menu("LEVEL_CHOICES"):  # absent/"auto" = Jev or the planner picks it
        opts["level"] = level
    # "What do you want to add to each section?" (Quality+ only): known keys, in menu order; "auto" can't be combined
    # with the others. An empty list is kept: it means None (no worked examples), unlike a missing field (not asked).
    extras = _get(body, "extras")
    if quality in ("quality-plus", "quality-deep", "scene-plus", "code-lab", "code-lab-2", "code-lab-3", "code-lab-7", "playground") and isinstance(extras, list):
        picked = [k for k in data.menu("EXTRAS") if k in extras]
        opts["extras"] = ["auto"] if "auto" in picked else picked
    # Review rounds (the composer's Options): the most verify -> audit -> improve rounds (deepen.py). Absent or invalid =
    # deepen.MAX_ROUNDS.
    rounds = _get(body, "reviewRounds")
    # Not Lite+ (Quick read stays a short static page), Smart (its picks would be undone by Quality+ improves) or Code Lab
    # 1-3. Code Lab 7.0 and Playground can: an improve keeps their interactive parts as placeholders and puts them back (deepen.restore_widgets).
    if quality not in ("lite-plus", "smart", "code-lab", "code-lab-2", "code-lab-3") and isinstance(rounds, int) and not isinstance(rounds, bool) and 1 <= rounds <= ROUNDS_CEILING:
        opts["reviewRounds"] = rounds
    # Code Lab 2.0, 3.0, 7.0 and Playground only: plan (and, in 2.0-3.0, repair and back up) charts with Haiku's thinking on (True) or off (False). Absent = on.
    think = _get(body, "planThinking")
    if quality in ("code-lab-2", "code-lab-3", "code-lab-7", "playground") and isinstance(think, bool):
        opts["planThinking"] = think
    # Page colours (the composer's Options): the page opens and exports in light or dark. Absent = follow the reader's setting.
    color = _get(body, "colorMode")
    if color in ("light", "dark"):
        opts["colorMode"] = color
    # Quality 2.0 test runs (scripts/variety.mjs): every version and audit in the done event. The app never sends it.
    if quality == "quality-deep" and _get(body, "debugVersions") is True:
        opts["debugVersions"] = True
    # Code Lab test runs (scripts/variety.mjs): the widgets Jev's review rejected, in the done event. The app never sends it.
    if quality in ("code-lab", "code-lab-2", "code-lab-3", "code-lab-7", "playground") and _get(body, "debugRejected") is True:
        opts["debugRejected"] = True
    # A follow-up in a conversation: revise the explainer above, or a related new one.
    f = parse_follow_up(_get(body, "followUp"))
    if isinstance(f, str):
        return f

    mode = _get(body, "mode")
    if mode == "concept":
        text = js_trim(body["text"]) if isinstance(body.get("text"), str) else ""
        if not text:
            return "Describe the concept you want explained."
        if js_len(text) > limits["conceptChars"]:
            return f"Please keep the description under {limits['conceptChars']} characters."
        return {"mode": "concept", "text": text, "quality": quality, **_order(opts), **({"followUp": f} if f else {})}
    if mode == "paper":
        sections = body.get("sections") if isinstance(body.get("sections"), list) else []
        if not sections or len(sections) > limits["sections"]:
            return "The paper has no readable sections."
        total = 0
        for s in sections:
            if not isinstance(s, dict) or not isinstance(s.get("heading"), str) or not isinstance(s.get("text"), str):
                return "Malformed paper sections."
            total += js_len(s["text"])
        if total > limits["paperChars"]:
            return "The paper is too long."
        title = body.get("title")
        note = body.get("note")
        return {
            "mode": "paper",
            "title": js_slice(js_string("Untitled paper" if title is None else title), 0, 300),
            "note": js_slice(js_string("" if note is None else note), 0, 1000),
            "sections": [{"heading": js_slice(s["heading"], 0, 200), "text": s["text"]} for s in sections],
            "quality": quality,
            **_order(opts),
            **({"followUp": f} if f and f["kind"] == "new" else {}),  # a new paper can't "revise" the explainer above
        }
    return "Unknown request."


def _order(opts: dict) -> dict:
    return {k: opts[k] for k in ("effort", "interactivity", "level", "extras", "reviewRounds", "planThinking", "colorMode", "debugVersions", "debugRejected") if k in opts}


def parse_chat(body: Any) -> dict | str:
    limits = data.limits()
    msgs = _get(body, "messages")
    if not isinstance(msgs, list) or not msgs or len(msgs) > limits["chatMessages"]:
        return "Invalid messages."
    chars = 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            return "Invalid messages."
        chars += js_len(m["content"])
    if msgs[-1]["role"] != "user":
        return "Last message must be from the user."
    # body.systemPrompt is ignored: the server builds the system prompt (audit F03).
    ctx = body.get("context")
    context = js_slice(js_string("" if ctx is None else ctx), 0, 30_000)
    if chars + js_len(context) > limits["chatChars"] + 30_000:
        return "Conversation too long."
    return {"context": context, "messages": [{"role": m["role"], "content": m["content"]} for m in msgs]}
