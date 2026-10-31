"""Revisions as changes (Quality+): the planner replies with only what changes, and code merges it into the
previous outline, so a request like "add a section on X" writes one stage instead of the whole page.

The reply is {"changes": {...}} (core/followup.py's revision_request describes it to the planner):
  add      [{"after": "<stage id>" | "start", "stage": {...}}]   new stages, placed after a kept stage
  replace  [{...stage with an existing id...}]                     rewritten stages
  remove   ["<stage id>", ...]
  title, subtitle, keyIntuition, level                             new values for the page fields
  glossary [{"term", "definition"}]                                terms added or updated (matched by term)
Every stage it doesn't mention is copied from the previous outline as it was, so its wording can't drift.
A reply with "stages" instead is a complete outline (the planner judged the request too broad for changes).

The merged outline then goes through parse_outline like any plan. Which stages changed (by id) is returned,
so the later steps (worked examples, clarity check, fact check, section extras) run on those stages only.
"""

from __future__ import annotations

import re
from typing import Any

from .core.spec import parse_outline
from .js import js_trim

PAGE_FIELDS = ("title", "subtitle", "keyIntuition", "level")


def _id_of(stage: dict, taken: set[str], fallback: str) -> str:
    """The stage's id, made unique among the page's stages (a new stage that reuses a kept id gets a suffix)."""
    base = stage.get("id") if isinstance(stage.get("id"), str) and stage["id"].strip() else ""
    if not base:
        label = stage.get("label") if isinstance(stage.get("label"), str) else ""
        base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or fallback
    out, n = base, 2
    while out in taken:
        out = f"{base}-{n}"
        n += 1
    return out


def merge_revision(old: dict, changes: Any) -> dict:
    """{"outline": raw merged outline, "changed": [stage ids], "removed": int, "warnings": [...]}, or {"error": ...}."""
    if not isinstance(changes, dict):
        return {"error": '"changes" is not an object'}
    warnings: list[str] = []
    stages = [dict(s) for s in old.get("stages", []) if isinstance(s, dict)]
    ids = [s.get("id") for s in stages]
    changed: list[str] = []

    removed = 0
    for rid in changes.get("remove") if isinstance(changes.get("remove"), list) else []:
        if rid in ids:
            i = ids.index(rid)
            del stages[i], ids[i]
            removed += 1
        else:
            warnings.append(f'Revision: no stage "{rid}" to remove.')

    for st in changes.get("replace") if isinstance(changes.get("replace"), list) else []:
        if not isinstance(st, dict):
            continue
        if st.get("id") in ids:
            stages[ids.index(st["id"])] = st
            changed.append(st["id"])
        else:  # an id the page doesn't have: keep the stage, as an addition at the end
            sid = _id_of(st, set(ids), f"stage-{len(ids) + 1}")
            stages.append({**st, "id": sid})
            ids.append(sid)
            changed.append(sid)
            warnings.append(f'Revision: no stage "{st.get("id")}" to replace, so "{st.get("label") or sid}" was added at the end.')

    for item in changes.get("add") if isinstance(changes.get("add"), list) else []:
        st = item.get("stage") if isinstance(item, dict) else None
        if not isinstance(st, dict):
            continue
        sid = _id_of(st, set(ids), f"stage-{len(ids) + 1}")
        after = item.get("after")
        if after in (None, "", "start"):
            at = 0
        elif after in ids:
            at = ids.index(after) + 1
        else:
            at = len(ids)
            warnings.append(f'Revision: no stage "{after}" to add "{st.get("label") or sid}" after, so it was added at the end.')
        stages.insert(at, {**st, "id": sid})
        ids.insert(at, sid)
        changed.append(sid)

    fields = {k: changes[k] for k in PAGE_FIELDS if isinstance(changes.get(k), str) and changes[k].strip()}
    glossary = [dict(g) for g in old.get("glossary", []) if isinstance(g, dict)]
    added_terms = 0
    for g in changes.get("glossary") if isinstance(changes.get("glossary"), list) else []:
        if not (isinstance(g, dict) and isinstance(g.get("term"), str) and isinstance(g.get("definition"), str)):
            continue
        same = next((x for x in glossary if str(x.get("term", "")).lower() == g["term"].lower()), None)
        if same:
            same["definition"] = g["definition"]
        else:
            glossary.append({"term": g["term"], "definition": g["definition"]})
        added_terms += 1

    if not stages:
        return {"error": "the changes remove every stage"}
    if not (changed or removed or fields or added_terms):
        return {"error": "the reply has no changes"}
    return {"outline": {**old, **fields, "stages": stages, "glossary": glossary},
            "changed": list(dict.fromkeys(changed)), "removed": removed, "warnings": warnings}


def revision_summary(revision: dict, outline: dict, old: dict) -> dict:
    """{"counts": spec["revision"], "text": the plan step's detail}, e.g. "2 sections added, 7 kept as they were".
    A follow-up that changed interactive parts (targets.py) lists them in revision["widgets"] (stage ids): they are counted as
    "charts" and their stages aren't "kept" unless their text was, too."""
    old_ids = {s.get("id") for s in old.get("stages", []) if isinstance(s, dict)}
    final = {s["id"] for s in outline["stages"]}
    changed = [i for i in revision["changed"] if i in final]
    charts = [i for i in revision.get("widgets", []) if i in final]
    counts = {"added": sum(1 for i in changed if i not in old_ids), "replaced": sum(1 for i in changed if i in old_ids),
              "removed": revision["removed"], "kept": len(final) - len(set(changed) | set(charts))}
    if charts:
        counts["charts"] = len(charts)

    def n(k: int, what: str) -> str:
        return f"{k} section{'' if k == 1 else 's'} {what}"

    parts = [n(counts["added"], "added") if counts["added"] else "", n(counts["replaced"], "rewritten") if counts["replaced"] else "",
             n(counts["removed"], "removed") if counts["removed"] else "",
             f"{len(charts)} interactive part{'' if len(charts) == 1 else 's'} changed" if charts else "", f"{counts['kept']} kept as they were"]
    return {"counts": counts, "text": ", ".join(p for p in parts if p)}


def parse_revision(raw: Any, old: dict) -> dict:
    """The parse step for a Quality+ revision (pipeline.write_outline): changes merged into `old`, or a complete
    outline. {"outline", "warnings", "revision"?} or {"error"}; "revision" is present only for changes."""
    if isinstance(raw, dict) and "changes" in raw and "stages" not in raw:
        m = merge_revision(old, raw["changes"])
        if "error" in m:
            return m
        p = parse_outline(m["outline"])
        if "error" in p:
            return p
        kept_ids = {s["id"] for s in p["outline"]["stages"]}
        changed = [i for i in m["changed"] if i in kept_ids]  # a stage parse_outline dropped isn't "changed"
        return {"outline": p["outline"], "warnings": [*m["warnings"], *p["warnings"]],
                "revision": {"changed": changed, "removed": m["removed"]}}
    return parse_outline(raw)


# ---------- Quality 2.0's improve request (deepen.py) ----------


def page_block(f: dict) -> dict:
    """The page as the first user block, marked for caching. The audit call and the improve plan both start with
    this exact block (after the same tools and system prompt), so the improve plan reads the page from the cache the
    audit wrote. Build both from the same follow-up so the bytes match."""
    text = f"Current explainer (outline JSON):\n{f['outline']}"
    if f.get("source"):
        text += f"\n\nSource material it was built from (stay accurate to it):\n{f['source']}"
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def improve_request(f: dict, rules: str, hint: str = "") -> list[dict]:
    """The improve plan's user turn: the cached page block, then the fix list (f["improve"]) and the change rules."""
    task = "\n\n".join(p for p in [f["improve"], rules, hint] if p)
    return [page_block(f), {"type": "text", "text": task}]


def _placeholder(b: dict) -> str:
    """The text a revision (and an improve, deepen.placeholder) sees in place of a widget."""
    return f"(interactive widget: {b.get('title', '')})"


def restore_kept_widgets(outline: dict, widgets: dict[str, list[dict]]) -> dict:
    """Put a follow-up's kept widgets (FollowUp.widgets, by stage id) back where their placeholders survived.
    A widget goes back into its own stage first, in order (matched by title); a placeholder that moved to another
    stage, or sits in a rewritten one, takes any leftover widget with that title. A placeholder the revision
    dropped stays dropped: the widget is not forced back in."""
    spare = {sid: list(ws) for sid, ws in widgets.items()}

    def take(pool: list[dict], text: str) -> dict | None:
        for i, w in enumerate(pool):
            if _placeholder(w) == text:
                return pool.pop(i)
        return None

    def is_placeholder(b: dict) -> bool:
        return b.get("type") == "text" and isinstance(b.get("md"), str)

    stages = []
    pending: list[tuple[int, int, str]] = []  # (stage, block, placeholder) not found in their own stage
    for si, st in enumerate(outline["stages"]):
        blocks = list(st["blocks"])
        for bi, b in enumerate(blocks):
            if not is_placeholder(b):
                continue
            w = take(spare.get(st["id"], []), js_trim(b["md"]))
            if w:
                blocks[bi] = w
            else:
                pending.append((si, bi, js_trim(b["md"])))
        stages.append({**st, "blocks": blocks})
    for si, bi, text in pending:  # the rest: any stage's leftover widget with that title
        for pool in spare.values():
            w = take(pool, text)
            if w:
                stages[si]["blocks"][bi] = w
                break
    return {**outline, "stages": stages}
