"""Section notes ("Add to each section": Deeper explanation, Same explanation in different wording), after the
worked examples: Haiku writes them per stage, in parallel chunks, and they go in spec.sectionNotes (stage id ->
{deeper, another}). The page opens them from each section's buttons in a side panel; the section's own text is
never changed. Never raises: a failed chunk only leaves its stages without notes, with a warning."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import anthropic

from . import data
from .js import js_slice
from .json_util import extract_json
from .llm import write_text
from .pricing import UsageLog

log = logging.getLogger(__name__)

# Composer choice -> the note's field.
NOTE_FIELDS = {"depth": "deeper", "rephrase": "another"}
CHUNK = 3  # stages per Haiku call
STAGE_CHARS = 2500


def note_fields(extras: list[str] | None) -> list[str]:
    return [f for k, f in NOTE_FIELDS.items() if k in (extras or [])]


def _stage_text(st: dict) -> str:
    parts = [st.get("blurb", "")]
    for b in st["blocks"]:
        if b["type"] == "text":
            parts.append(b["md"])
        elif b["type"] == "callout":
            parts.append(f"{b['title']}: {b['md']}" if b.get("title") else b["md"])
        elif b["type"] == "formula":
            parts.append(f"$${b['latex']}$$ {b.get('caption', '')}".strip())
    return js_slice("\n\n".join(p for p in parts if p), 0, STAGE_CHARS)


def _chunk(client: anthropic.Anthropic, spec: dict, idx: list[int], fields: list[str], usage: UsageLog) -> dict[str, dict]:
    level = data.menu("LEVELS").get(spec["level"], spec["level"])
    names = {"deeper": '"deeper" (a deeper explanation)', "another": '"another" (the same explanation in different wording)'}
    user = "\n\n".join([
        f"Explainer: {spec['title']}\nReader level: {level}\nWrite for every stage: {' and '.join(names[f] for f in fields)}.",
        *(f"Stage {i}: {spec['stages'][i]['label']}\n{_stage_text(spec['stages'][i])}" for i in idx),
    ])
    text = write_text(client, usage, "section notes", system=data.prompt("SECTION_NOTES_SYSTEM"), user=user,
                      max_tokens=3000 * len(idx), fmt=data.formats()["sectionNotes"])
    value, _ = extract_json(text)
    items = [x for x in (value.get("stages", []) if isinstance(value, dict) else []) if isinstance(x, dict)]
    # Indices that match none of this chunk's stages (counted from 0 or 1 within the chunk): match by position.
    if items and not any(x.get("index") in idx for x in items) and len(items) == len(idx):
        items = [{**x, "index": i} for x, i in zip(items, idx)]
    out: dict[str, dict] = {}
    for item in items:
        i = item.get("index")
        if i not in idx:
            continue
        note = {f: item[f].strip() for f in fields if isinstance(item.get(f), str) and item[f].strip()}
        if note:
            out[spec["stages"][i]["id"]] = note
    if not out:
        log.warning("section notes: nothing usable for stages %s; reply starts %r", idx, text[:300])
    return out


def write_notes(client: anthropic.Anthropic, spec: dict, extras: list[str] | None, usage: UsageLog,
                kept: dict[str, dict] | None = None) -> tuple[dict, str]:
    """(spec with sectionNotes, step detail). kept: notes to reuse by stage id (a revision's unchanged stages);
    a stage whose kept note has every requested field isn't written again."""
    fields = note_fields(extras)
    if not fields or not spec["stages"]:
        return spec, ""
    ids = {st["id"] for st in spec["stages"]}
    notes: dict[str, dict] = {sid: n for sid, n in (kept or {}).items() if sid in ids and all(n.get(f) for f in fields)}
    todo = [i for i, st in enumerate(spec["stages"]) if st["id"] not in notes]
    if not todo:
        return {**spec, "sectionNotes": notes}, f"{len(notes)} of {len(spec['stages'])} sections (all kept)"
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [pool.submit(_chunk, client, spec, c, fields, usage) for c in chunks]
        for c, fut in zip(chunks, futures):
            try:
                notes.update(fut.result())
            except Exception as e:  # noqa: BLE001 - extras must never fail the page
                warnings.append(f"Section extras for stages {c[0] + 1}-{c[-1] + 1} couldn't be written ({type(e).__name__}).")
    missing = sum(1 for st in spec["stages"] if st["id"] not in notes)
    if missing and not warnings:
        warnings.append(f"{missing} stage(s) got no section extras.")
    spec = {**spec, "sectionNotes": notes, "warnings": [*spec["warnings"], *warnings]} if notes else {**spec, "warnings": [*spec["warnings"], *warnings]}
    reused = len(spec["stages"]) - len(todo)
    return spec, f"{len(notes)} of {len(spec['stages'])} sections" + (f" ({reused} kept)" if reused else "")
