"""Port of shared/lab.ts: Freeform+ planning, where the planner writes `interactive` briefs
(goal + math + insight) and Haiku then writes each one as a sandboxed widget."""

from __future__ import annotations

from typing import Any

from .. import data
from ..js import js_str
from .schema import safe_parse
from .spec import coerce_outline, normalize_block, parse_block, type_name


def _issue_text(issue: dict | None) -> str:
    if not issue:
        return "invalid"
    path = ".".join(js_str(p) if not isinstance(p, str) else p for p in issue["path"])
    return f"{path or 'block'}: {issue['message']}"


def parse_lab_outline(raw: Any) -> dict:
    """{"outline": ..., "warnings": [...]} or {"error": ...}; blocks may be briefs or regular blocks."""
    loose = coerce_outline(raw)
    if "error" in loose:
        return loose
    warnings = loose["warnings"]
    brief_schema = data.schemas()["brief"]
    stages = []
    for st in loose["data"]["stages"]:
        blocks = []
        for i, b in enumerate(st["blocks"]):
            n = normalize_block(b)
            ok_brief, brief = safe_parse(brief_schema, n)
            if ok_brief:
                blocks.append(brief)
                continue
            ok_block, block = parse_block(n)
            if ok_block:
                blocks.append(block)
                continue
            t = b.get("type") if isinstance(b, dict) else None
            issues = brief if t == "interactive" else block
            warnings.append(f'Dropped block {i + 1} ("{type_name(b)}") in "{st["label"]}": {_issue_text(issues[0] if issues else None)}.')
        stages.append({**st, "blocks": blocks})
    return {"outline": {**loose["data"], "stages": stages}, "warnings": warnings}


def briefs_of(o: dict) -> list[dict]:
    out: list[dict] = []
    for si, st in enumerate(o["stages"]):
        for bi, b in enumerate(st["blocks"]):
            if b.get("type") == "interactive":
                out.append({"key": f"b{len(out) + 1}", "stageIndex": si, "blockIndex": bi, "stageLabel": st["label"], "brief": b})
    return out


def merge_bundles(o: dict, bundles: dict[str, list[dict]]) -> dict:
    """Swap every brief for its built blocks (or drop it) to get a normal outline."""
    key_at = {(r["stageIndex"], r["blockIndex"]): r["key"] for r in briefs_of(o)}
    stages = []
    for si, st in enumerate(o["stages"]):
        blocks: list[dict] = []
        for bi, b in enumerate(st["blocks"]):
            if b.get("type") == "interactive":
                blocks.extend(bundles.get(key_at[(si, bi)], []))
            else:
                blocks.append(b)
        stages.append({**st, "blocks": blocks})
    return {**o, "stages": stages}
