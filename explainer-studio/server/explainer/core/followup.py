"""Port of the server half of shared/followup.ts: a follow-up turned into the planner's user turn
(the cached system prompts stay byte-stable). The browser builds the FollowUp itself."""

from __future__ import annotations


# How a revision that only changes part of the page replies (Quality+; server/explainer/revise.py merges it).
CHANGE_RULES = "\n".join([
    "Rules for the revision:",
    '- Reply with one JSON object holding only what changes: {"changes": {...}}. Every stage you don\'t mention is kept exactly as it is, so never repeat an unchanged stage. Its fields, each optional:',
    '  - "add": [{"after": "<id of the stage it follows, or \\"start\\">", "stage": {a complete new stage in the usual format, with a new id}}]',
    '  - "replace": [{a complete stage in the usual format, with the id of the stage it replaces}]',
    '  - "remove": ["<stage id>", ...]',
    '  - "title", "subtitle", "keyIntuition", "level": new values, only when they should change',
    '  - "glossary": [{"term": "...", "definition": "..."}]: terms to add or update',
    "- Change what the request asks for, and anything the change makes wrong. A new or replaced stage may use the controls of the stages before it and must use one of the outline's phases.",
    "- If the request changes most of the explainer (a different level, audience, style or length throughout), reply instead with the complete revised outline in the usual format, keeping the ids of the stages you keep.",
    "- Use the reader level the request implies, otherwise keep the current one.",
])

REWRITE_RULES = "\n".join([
    "Rules for the revision:",
    "- Reply with the complete revised outline as one JSON object, in the usual format.",
    "- Change what the request asks for. Keep everything else (stages, blocks, numbers, formulas, wording) as it is unless the change makes it wrong or the request asks for a broader rewrite.",
    "- Keep the ids of the stages you keep. Use the reader level the request implies, otherwise keep the current one.",
])


# Added when the outline holds widget placeholders: the finished widget is put back where its placeholder is.
WIDGET_RULE = 'A text block that reads "(interactive widget: ...)" stands for a finished interactive widget. Keep every such block exactly as it is and where it is; widgets can\'t be rewritten here, so fix the text around them instead.'


def revision_request(text: str, f: dict, changes: bool = False) -> str:
    """changes: the reply may hold only the changes (Quality+); otherwise the complete revised outline (Lite+, Freeform+)."""
    parts = [
        "Revise an existing interactive explainer for the reader's follow-up request.",
        f"Reader's request: {text}",
        CHANGE_RULES if changes else REWRITE_RULES,
        WIDGET_RULE if "(interactive widget: " in f["outline"] else "",
        f"Current explainer (outline JSON):\n{f['outline']}",
        f"Source material it was built from (stay accurate to it):\n{f['source']}" if f.get("source") else "",
    ]
    return "\n\n".join(p for p in parts if p)


def continuation_note(f: dict) -> str:
    parts = [
        f"This continues a conversation that already produced these explainers:\n{f['previous']}",
        "Build on them: reuse their terms, refer back where it helps, and don't re-teach what they covered at length unless the request asks for it.",
        f"Source material from the conversation (use it when the request is about the same material):\n{f['source']}" if f.get("source") else "",
    ]
    return "\n\n".join(p for p in parts if p)
