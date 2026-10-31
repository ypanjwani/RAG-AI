"""Edit chat: one Haiku turn per request. Haiku reads the reader's message and the selected part's
description, then answers and/or calls tools. The tools run in the browser (app/src/shell/edit.ts,
over shared/edit.ts), where every change goes through checkEdit; their results come back in the
next request. So the server holds no state and never touches the page itself."""

from __future__ import annotations

from typing import Any

import anthropic

from . import data
from .js import js_len, json_stringify
from .llm import HAIKU
from .pricing import price_of


def parse_edit(body: Any) -> dict | str:
    """A /api/edit body, validated; a string is the error to show."""
    body = body if isinstance(body, dict) else {}
    context = body.get("context") if isinstance(body.get("context"), str) else ""
    msgs = body.get("messages")
    limits = data.limits()
    if not context or not isinstance(msgs, list) or not msgs or len(msgs) > limits["editMessages"]:
        return "Invalid edit request."
    if js_len(context) + js_len(json_stringify(msgs)) > limits["editChars"]:
        return "This conversation is too long. Close Edit chat and start again on the part."

    def content(c: Any) -> dict | None:
        if not isinstance(c, dict):
            return None
        if c.get("type") == "text" and isinstance(c.get("text"), str):
            return {"type": "text", "text": c["text"]}
        if c.get("type") == "tool_use" and isinstance(c.get("id"), str) and isinstance(c.get("name"), str) and isinstance(c.get("input"), (dict, list)) and c["input"] is not None:
            return {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
        if c.get("type") == "tool_result" and isinstance(c.get("tool_use_id"), str) and isinstance(c.get("content"), str):
            out = {"type": "tool_result", "tool_use_id": c["tool_use_id"], "content": c["content"]}
            if c.get("is_error"):
                out["is_error"] = True
            return out
        return None

    messages: list[dict] = []
    for m in msgs:
        role = m.get("role") if isinstance(m, dict) else None
        if role not in ("user", "assistant"):
            return "Invalid edit request."
        if isinstance(m.get("content"), str):
            messages.append({"role": role, "content": m["content"]})
            continue
        if not isinstance(m.get("content"), list):
            return "Invalid edit request."
        parts = [content(c) for c in m["content"]]
        if any(p is None for p in parts):
            return "Invalid edit request."
        messages.append({"role": role, "content": parts})
    if messages[-1]["role"] != "user":
        return "Invalid edit request."
    return {"context": context, "messages": messages}


def edit_turn(client: anthropic.Anthropic, req: dict) -> dict:
    msg = client.messages.create(
        model=HAIKU,
        max_tokens=4000,
        output_config={"effort": "low"},
        tools=data.formats()["editTools"],
        tool_choice={"type": "auto"},
        # The instructions and tools are cached; the part's description changes every turn.
        system=[
            {"type": "text", "text": data.prompt("EDIT_SYSTEM"), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"The selected part:\n{req['context']}"},
        ],
        messages=req["messages"],
    )
    usd = price_of(msg.model, msg.usage)
    if msg.stop_reason == "refusal":
        return {"content": [{"type": "text", "text": "Sorry, I can't help with that one."}], "usd": usd}
    content = []
    for c in msg.content:
        if c.type == "text":
            content.append({"type": "text", "text": c.text})
        elif c.type == "tool_use":
            content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
    return {"content": content, "usd": usd}
