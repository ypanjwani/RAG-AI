"""Feedback, steps 1 and 2: a summary of every section, built by code from the page's JSON, and Haiku deciding which
sections the feedback asks to change.

section_cards: one short card per section, read straight from the spec: its number, name and description, a line per block
(chart and bar titles, slider labels, readouts, the start of each text) and, for each interactive widget, its controls
(sliders, Play, toggles, buttons, dragging on a chart, and the Reset every playground gets automatically) and what it shows,
from the plan stored on the widget. A widget whose only button is Reset is flagged as such. The change the previous feedback
made is noted on the sections it touched. No AI call, so it costs nothing and always matches the page.

pick_sections: one small Haiku call (FEEDBACK_PICK_SYSTEM) with the cards and the feedback: the sections the feedback asks to
change (also when the reader describes a kind of part instead of naming it, e.g. "the widgets that only have Reset"), the
sections it might be about when it can't tell, and the kind of feedback (parts / whole page / new section / no change).
feedback.decide_scope turns that into what the plan step may change. Returns None when the reply is unusable, and the plan
step then picks among all sections itself.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import anthropic

from . import codelab_edit as ce
from . import data
from .js import js_slice, json_parse
from .llm import HAIKU, cached_system, text_of
from .pricing import price_of
from .labplan import block_text

log = logging.getLogger("explainer.feedback")

KINDS = ("parts", "page", "new", "none")
SNIPPET = 120
MAX_CARD_SECTIONS = 20
CONTROL_TYPES = ("slider", "select", "toggle", "button", "play")  # the playground library's controls (app/public/viewer.js)


def _clip(s: Any, n: int = SNIPPET) -> str:
    return js_slice(re.sub(r"\s+", " ", str(s or "")).strip(), 0, n)


def _labels(items: Any, key: str = "label") -> list[str]:
    return [_clip(x.get(key), 60) for x in items or [] if isinstance(x, dict) and _clip(x.get(key), 60)]


# What a playground chart draws, from the helpers its draw code calls (the library's bars(), curve(), point(), arrow(), matrix()...).
DRAWS = (("bar", "bar"), ("curve", "line"), ("line", "line"), ("path", "line"), ("area", "line"), ("matrix", "grid"),
         ("grid", "grid"), ("heat", "grid"), ("point", "point"), ("arrow", "arrow"))


def chart_kind(draw: Any) -> str:
    """ "bar", "line and point" etc. for a playground chart's draw code; "" when it can't be told."""
    called = {w.lower() for w in re.findall(r"\b([A-Za-z]+)\s*\(", str(draw or ""))}
    kinds = list(dict.fromkeys(k for prefix, k in DRAWS if any(w.startswith(prefix) for w in called)))
    return " and ".join(kinds[:3])


def playground_visuals(b: dict) -> list[str]:
    """Each chart of a playground as the reader sees it: its title and what it draws, e.g. 'Per-head dimension (bar chart)'."""
    spec = ce.read_playground_spec(b.get("html", "")) or {}
    out = []
    for c in spec.get("components") or []:
        if isinstance(c, dict) and c.get("type") == "chart":
            kind = chart_kind(c.get("draw"))
            title = _clip(c.get("title") or c.get("id"), 60)
            out.append(f"{title} ({kind} chart)" if kind else f"{title} (chart)")
        elif isinstance(c, dict) and c.get("type") == "readout" and c.get("label"):
            out.append(f"{_clip(c['label'], 40)} (readout)")
    return out


def _playground_controls(b: dict) -> tuple[list[str], bool, bool] | None:
    """(the controls a reader can press or slide, whether a chart can be dragged or clicked, whether it's a playground), from
    the playground spec in the html; None when it isn't a playground."""
    if ce.widget_kind(b) != "playground":
        return None
    spec = ce.read_playground_spec(b.get("html", "")) or {}
    comps = [c for c in spec.get("components") or [] if isinstance(c, dict)]
    controls = [f"{c.get('label') or c.get('id')} ({c['type']})" if c.get("type") != "slider" else str(c.get("label") or c.get("id"))
                for c in comps if c.get("type") in CONTROL_TYPES and c.get("action") != "reset"]
    drag = any(c.get("type") == "chart" and (c.get("onDrag") or c.get("onClick")) for c in comps)
    return controls, drag, True


def widget_line(b: dict) -> str:
    """A widget's controls and what it shows: for a playground from its spec (plus the Reset the library always adds),
    otherwise from its stored plan."""
    plan = ce.read_plan(b)
    shows = _labels(plan.get("visuals"), "what")
    pg = _playground_controls(b)
    if pg is not None:
        shows = playground_visuals(b) or shows
        controls, drag, _ = pg
        items = controls + (["dragging on a chart"] if drag else []) + ["Reset (added automatically)"]
        flag = "" if controls else "; its only button is Reset" + (": the reader can only drag on its chart" if drag else ": nothing else to press")
    else:
        items = [f"{c.get('label')} ({c.get('kind')})" if c.get("kind") and c.get("kind") != "slider" else str(c.get("label"))
                 for c in plan.get("controls") or [] if isinstance(c, dict) and c.get("label")]
        flag = ""
    out = f'interactive widget "{_clip(b.get("title"), 80)}"'
    if items:
        out += f"; controls: {', '.join(items[:10])}"
    out += flag
    if shows:
        out += f"; shows: {', '.join(shows[:8])}"
    if plan.get("lesson"):
        out += f"; teaches: {_clip(plan['lesson'])}"
    return out


def block_line(b: dict) -> str:
    """One block, as the reader would recognise it on the page."""
    t = b.get("type")
    if t == "widget":
        return widget_line(b)
    if t == "chart":
        series = _labels(b.get("series"))
        return f'line chart "{_clip(b.get("title"), 80)}"' + (f" (lines: {', '.join(series[:4])})" if series else "")
    if t == "bars":
        return f'bar chart "{_clip(b.get("title"), 80)}"' + (f" (bars: {', '.join(str(x) for x in (b.get('labels') or [])[:6])})" if b.get("labels") else "")
    if t == "slider":
        return f'slider "{_clip(b.get("label"), 60)}"'
    if t == "readout":
        return "readout: " + ", ".join(_labels(b.get("items"))[:4])
    if t == "formula":
        return "formula: " + _clip(b.get("latex"), 80)
    words = block_text(b)
    if words:
        return f"{t}: {_clip(words)}"
    title = b.get("title") or b.get("prompt") or b.get("label")
    return f"{t}" + (f' "{_clip(title, 80)}"' if title else "")


def section_cards(page: dict, last: dict | None = None) -> list[dict]:
    """[{number, id, label, summary, parts, lastChange?}] for every section. last: {"stages", "what"} of the previous feedback."""
    touched = set((last or {}).get("stages") or [])
    cards = []
    for i, st in enumerate(page["stages"][:MAX_CARD_SECTIONS]):
        card = {"number": i + 1, "id": st["id"], "label": str(st.get("label") or st["id"]), "summary": _clip(st.get("blurb"), 200),
                "parts": [block_line(b) for b in st.get("blocks") or [] if isinstance(b, dict)]}
        if st["id"] in touched and (last or {}).get("what"):
            card["lastChange"] = f"The reader's previous feedback changed this section: {_clip(last['what'], 240)}"
        cards.append(card)
    return cards


def cards_text(cards: list[dict]) -> str:
    """The cards for the writer, one block per section."""
    out = []
    for c in cards:
        lines = [f'Section {c["number"]}, id "{c["id"]}": "{c["label"]}"' + (f" ({c['summary']})" if c["summary"] else "")]
        lines += [f"  - {p}" for p in c["parts"]]
        if c.get("lastChange"):
            lines.append(f"  * {c['lastChange']}")
        out.append("\n".join(lines))
    return "\n".join(out)


def decide(raw: Any, cards: list[dict]) -> dict | None:
    """{chosen: [ids], maybe: [ids], kind} from the picker's reply, ids checked against the page (in page order); None when
    the reply is unusable."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        return None
    ids = [c["id"] for c in cards]
    want = {s for s in raw.get("sections") or [] if isinstance(s, str)}
    unsure = {s for s in raw.get("unsure") or [] if isinstance(s, str)} - want
    return {"chosen": [i for i in ids if i in want], "maybe": [i for i in ids if i in unsure], "kind": raw["kind"],
            "why": js_slice(" ".join(str(raw.get("why") or "").split()), 0, 300)}


def pick_sections(client: anthropic.Anthropic, cards: list[dict], text: str, answer: str = "") -> tuple[dict | None, float]:
    """(Haiku's pick, its cost). (None, cost) when the reply is unusable: the plan step then picks among all sections."""
    if not cards:
        return None, 0.0
    reply = f"\n\n<answer>\n{answer}\n</answer>" if answer else ""
    user = f"The page's sections:\n{cards_text(cards)}\n\n<feedback>\n{text}\n</feedback>{reply}"
    msg = client.messages.create(
        model=HAIKU,
        max_tokens=600,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": data.formats()["feedbackPick"]}},
        system=cached_system(data.prompt("FEEDBACK_PICK_SYSTEM")),
        messages=[{"role": "user", "content": user}],
    )
    usd = price_of(msg.model, msg.usage)
    try:
        pick = decide(json_parse(text_of(msg)), cards)
    except Exception:
        pick = None
    log.info("Feedback pick: %s", pick)
    return pick, usd
