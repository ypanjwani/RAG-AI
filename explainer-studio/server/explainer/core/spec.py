"""Port of the parsing half of shared/spec.ts: coerceOutline, normalizeBlock, parseOutline.

Blocks are plain dicts, as JSON gives them. A field that is "undefined" in the
TypeScript code is a missing key here.
"""

from __future__ import annotations

from typing import Any

from .. import data
from ..js import js_trim
from .schema import parse, safe_parse

CARD_VARIANTS = ["definition", "rule", "tip", "mistake", "claim", "keynumber"]


def _schemas() -> dict:
    return data.schemas()


def block_schema(t: str) -> dict | None:
    return _schemas()["blocks"].get(t)


def parse_block(b: Any) -> tuple[bool, Any]:
    """Block.safeParse: the first union option that matches (only the one whose type literal matches can)."""
    s = _schemas()
    if isinstance(b, dict) and isinstance(b.get("type"), str) and b["type"] in s["blocks"]:
        val, issues = parse(s["blocks"][b["type"]], b)
        if not issues:
            return True, val
    return False, [{"path": [], "message": "Invalid input"}]


def parse_design(d: Any) -> tuple[bool, Any]:
    return safe_parse(_schemas()["design"], d)


def _str(v: Any) -> str | None:
    if isinstance(v, str):
        t = js_trim(v)
        return t or None
    return None


def coerce_outline(raw: Any) -> dict:
    """{"data": loose outline, "warnings": [...]} or {"error": ...}. See coerceOutline in spec.ts."""
    if not isinstance(raw, dict):
        return {"error": "the reply is not a JSON object"}
    o = raw
    warnings: list[str] = []

    def fix(field: str, used: str) -> None:
        warnings.append(f'Outline field "{field}" was missing or invalid; used {used}.')

    stages: list[dict] = []
    for i, s in enumerate(o.get("stages") if isinstance(o.get("stages"), list) else []):
        st = s if isinstance(s, dict) else None
        label = (_str(st.get("label")) or _str(st.get("id"))) if st is not None else None
        if st is None or not label:
            warnings.append(f"Dropped stage {i + 1}: it had no label.")
            continue
        blocks = st.get("blocks")
        if not isinstance(blocks, list):
            warnings.append(f'Stage "{label}" had no block list.')
        stages.append({
            "id": _str(st.get("id")) or label,
            "label": label,
            "phase": _str(st.get("phase")) or "",
            "blurb": _str(st.get("blurb")) or "",
            "blocks": blocks if isinstance(blocks, list) else [],
        })
    if not stages:
        return {"error": "no usable stages"}

    levels = data.menu("LEVELS")
    layouts = data.menu("LAYOUTS")
    title = _str(o.get("title"))
    if not title:
        fix("title", '"Untitled explainer"')
    subtitle = _str(o.get("subtitle")) or ""
    level = o.get("level")
    if not (isinstance(level, str) and level in levels):
        fix("level", '"student"')
        level = "student"
    layout = o.get("layout")
    if not (isinstance(layout, str) and layout in layouts):
        fix("layout", '"article"')
        layout = "article"
    key_intuition = _str(o.get("keyIntuition"))
    if key_intuition is None:
        fix("keyIntuition", "the subtitle")
        key_intuition = subtitle
    phases = []
    for p in o.get("phases") if isinstance(o.get("phases"), list) else []:
        pid = _str(p.get("id")) if isinstance(p, dict) else None
        plabel = _str(p.get("label")) if isinstance(p, dict) else None
        if pid and plabel:
            phases.append({"id": pid, "label": plabel})
    glossary = []
    for g in o.get("glossary") if isinstance(o.get("glossary"), list) else []:
        term = _str(g.get("term")) if isinstance(g, dict) else None
        definition = _str(g.get("definition")) if isinstance(g, dict) else None
        if term and definition:
            glossary.append({"term": term, "definition": definition})
    return {
        "data": {
            "title": title or "Untitled explainer", "subtitle": subtitle, "level": level, "layout": layout,
            "keyIntuition": key_intuition, "phases": phases, "stages": stages, "glossary": glossary,
        },
        "warnings": warnings,
    }


def normalize_block(b: Any) -> Any:
    """{type: "tip", title, md} -> {type: "cards", variant: "tip", items: [...]}."""
    if not isinstance(b, dict) or not isinstance(b.get("type"), str) or b["type"] not in CARD_VARIANTS:
        return b
    item = {k: v for k, v in b.items() if k not in ("type", "title", "items")}
    title = b.get("title")
    items = b.get("items")
    if isinstance(items, list):
        return {"type": "cards", "variant": b["type"], "title": "" if title is None else title, "items": items}
    card = {"title": "" if title is None else title, "md": _coalesce(item.get("md"), item.get("text"), "")}
    card.update(item)
    return {"type": "cards", "variant": b["type"], "title": "", "items": [card]}


def _coalesce(*vals: Any) -> Any:
    """a ?? b ?? c (null and undefined both skip)."""
    for v in vals:
        if v is not None:
            return v
    return None


def type_name(b: Any) -> str:
    t = b.get("type") if isinstance(b, dict) else None
    return t if isinstance(t, str) else "unknown"


def parse_outline(raw: Any) -> dict:
    """{"outline": ..., "warnings": [...]} or {"error": ...}. Bad blocks are dropped with a warning."""
    loose = coerce_outline(raw)
    if "error" in loose:
        return loose
    warnings = loose["warnings"]
    stages = []
    for st in loose["data"]["stages"]:
        blocks = []
        for i, b in enumerate(st["blocks"]):
            ok, r = parse_block(normalize_block(b))
            if ok:
                blocks.append(r)
            else:
                warnings.append(f'Dropped block {i + 1} ("{type_name(b)}") in "{st["label"]}": {r[0]["message"] if r else "invalid"}.')
        stages.append({**st, "blocks": blocks})
    return {"outline": {**loose["data"], "stages": stages}, "warnings": warnings}
