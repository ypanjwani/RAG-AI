"""Section tools (POST /api/section): when the reader clicks "Search the web" on a section, one Haiku call
searches the web about it and writes a short summary with its sources. Nothing runs until the click, so the
button costs nothing on pages where nobody uses it.
Web text only ever reaches the reader as sanitized Markdown (mdToHtml) and as links."""

from __future__ import annotations

import html
from typing import Any

import anthropic

from . import data
from .js import js_len, js_trim
from .llm import HAIKU
from .pricing import price_of

WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches, on top of tokens
MAX_SEARCHES = 3  # per click; the prompt asks for one or two
MAX_CONTINUATIONS = 2  # pause_turn resumes of a long server-side search turn
MAX_SOURCES = 6


def parse_section(body: Any) -> dict | str:
    """A /api/section body, validated; a string is the error to show."""
    body = body if isinstance(body, dict) else {}
    limits = data.limits()
    kind = body.get("kind")
    if kind != "search":
        return "Unknown section tool."
    text = js_trim(body["text"]) if isinstance(body.get("text"), str) else ""
    if not text:
        return "This section has no text to work from."
    if js_len(text) > limits["sectionChars"]:
        return "This section is too long."
    title = body.get("title") if isinstance(body.get("title"), str) else ""
    stage = body.get("stage") if isinstance(body.get("stage"), str) else ""
    level = body.get("level") if body.get("level") in data.menu("LEVELS") else "beginner"
    prev = body.get("previous", [])
    if not isinstance(prev, list) or not all(isinstance(p, str) for p in prev):
        return "Invalid section request."
    if sum(js_len(p) for p in prev) > limits["sectionPrevious"]:
        return "Too many earlier answers for this section."
    return {"kind": kind, "title": title[:300], "stage": stage[:200], "level": level, "text": text, "previous": prev}


def _user_turn(req: dict) -> str:
    out = (f"Explainer: {req['title']}\nReader level: {data.menu('LEVELS')[req['level']]}\n"
           f"Section: {req['stage']}\n\nSection text:\n{req['text']}")
    if req["previous"]:
        out += "\n\nEarlier answers for this section:\n" + "\n\n---\n\n".join(req["previous"])
    return out


def _web_url(u: Any) -> str | None:
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else None


def section_turn(client: anthropic.Anthropic, req: dict) -> dict:
    """{"md", "sources", "searches", "usd"} for one click."""
    kwargs: dict[str, Any] = {
        "model": HAIKU,
        "max_tokens": 4000,
        "output_config": {"effort": "low"},
        "system": data.prompt("SECTION_SEARCH_SYSTEM"),
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}],
    }
    messages: list[dict] = [{"role": "user", "content": _user_turn(req)}]
    usd = 0.0
    searches = 0
    blocks: list[Any] = []
    for _ in range(MAX_CONTINUATIONS + 1):
        try:
            msg = client.messages.create(messages=messages, **kwargs)
        except Exception as e:
            # A continuation failed after earlier calls were paid: the route records what was spent (audit F23).
            e.spent_usd = usd + searches * WEB_SEARCH_USD  # type: ignore[attr-defined]
            raise
        usd += price_of(msg.model, msg.usage)
        stu = getattr(msg.usage, "server_tool_use", None)
        searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        blocks.extend(msg.content)
        if msg.stop_reason != "pause_turn":
            break
        # A paused server-side turn resumes when its message is sent back unchanged.
        messages = [*messages, {"role": "assistant", "content": msg.content}]
    usd += searches * WEB_SEARCH_USD
    if msg.stop_reason == "refusal":
        return {"md": "_Sorry, I can't help with this section._", "sources": [], "searches": searches, "usd": usd}

    # The answer is the text after the last search ("I'll search for..." before it is dropped).
    last_result = max((i for i, b in enumerate(blocks) if b.type == "web_search_tool_result"), default=-1)
    sources: list[dict] = []
    index: dict[str, int] = {}

    def cite(url: str, title: str) -> int:
        if url not in index and len(sources) < MAX_SOURCES:
            index[url] = len(sources) + 1
            sources.append({"title": title or url, "url": url})
        return index.get(url, 0)

    parts: list[str] = []
    for b in blocks[last_result + 1:]:
        if b.type != "text":
            continue
        marks = sorted({n for c in (getattr(b, "citations", None) or [])
                        if (u := _web_url(getattr(c, "url", None))) and (n := cite(u, getattr(c, "title", "") or ""))})
        parts.append(b.text + "".join(f'<sup><a href="{html.escape(sources[n - 1]["url"])}">{n}</a></sup>' for n in marks))
    # No citations (rare): list what the searches found.
    if not sources:
        for b in blocks:
            if b.type == "web_search_tool_result" and isinstance(b.content, list):
                for r in b.content:
                    if u := _web_url(getattr(r, "url", None)):
                        cite(u, getattr(r, "title", "") or "")
    md = "".join(parts).strip()
    if not md:
        md = "_The search found nothing useful about this section._"
    return {"md": md, "sources": sources, "searches": searches, "usd": usd}
