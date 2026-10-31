"""Shared Claude call helpers (Anthropic Python SDK)."""

from __future__ import annotations

from typing import Any

import anthropic

from .pricing import UsageLog

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"  # unused now: Code Lab writes its code with Haiku at High effort


def text_of(msg: Any) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")


def check_stop(msg: Any, what: str) -> None:
    if msg.stop_reason == "refusal":
        raise ReaderError(f"The model declined to write the {what}. Try rephrasing the request.")
    if msg.stop_reason == "max_tokens":
        raise ReaderError(f"The {what} was too long and got cut off. Try a narrower topic.")


def cached_system(text: str) -> list[dict]:
    """A system prompt as one cached block (the prompts are byte-stable, so the cache hits)."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def write_text(
    client: anthropic.Anthropic, usage: UsageLog, step: str, *, system: str, user: str | list[dict], max_tokens: int,
    fmt: dict | None = None, allow_cutoff: bool = False, effort: str = "low", model: str = HAIKU, stream: bool = False,
    thinking: bool = True,
) -> str:
    """One Haiku (or, with model=, another model) text-writing call (summaries, briefs to text, worked examples, fact check), optionally
    with structured output (fmt: a JSON Schema). Returns the reply text. stream=True reads the reply as a stream, which the SDK
    needs for max_tokens above ~21k (a non-streaming call that large is refused before it is sent). thinking=False turns
    Haiku's default adaptive thinking off (allowed at effort low, medium and high); thinking is billed as output."""
    output_config: dict = {"effort": effort}
    if fmt is not None:
        output_config["format"] = {"type": "json_schema", "schema": fmt}
    params: dict = dict(model=model, max_tokens=max_tokens, output_config=output_config, system=cached_system(system),
                        messages=[{"role": "user", "content": user}])
    if not thinking:
        params["thinking"] = {"type": "disabled"}
    if stream:
        with client.messages.stream(**params) as s:
            msg = s.get_final_message()
    else:
        msg = client.messages.create(**params)
    usage.add_message(step, msg)
    if not allow_cutoff:
        check_stop(msg, step)
    return text_of(msg)


class ReaderError(ValueError, RuntimeError):
    """An error whose text was written for the reader, so friendly_error may show it as is.
    Subclasses both ValueError and RuntimeError so existing `except` clauses keep catching it."""


def friendly_error(e: BaseException, request_id: str | None = None) -> str:
    """What the reader sees when a call fails. Only ReaderError text and known AI-service errors are shown;
    anything else (KeyErrors, database errors, paths, the AI service's 400 text) stays in the logs (audit F06)."""
    if isinstance(e, ReaderError):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "The AI service isn't set up correctly on the server. Please try again later."
    if isinstance(e, anthropic.RateLimitError):
        return "The AI service is busy right now. Please try again in a minute."
    if isinstance(e, anthropic.BadRequestError):
        return "The AI service couldn't handle this request. Try rephrasing it or making it shorter."
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the AI service. Please try again."
    if isinstance(e, anthropic.APIStatusError):
        return f"AI service error ({e.status_code}). Please try again."
    if isinstance(e, anthropic.APIError):
        return "AI service error. Please try again."
    ref = f" (reference {request_id})" if request_id else ""
    return f"Something went wrong on our side. Please try again{ref}."


def warning_reason(e: BaseException) -> str:
    """A short, safe reason for a page warning ("Fact check skipped: <reason>."), which readers see.
    Only text written for them passes; anything else is "an internal error" (the caller logs the details)."""
    from .jev import JevError  # jev doesn't import llm; imported here to keep llm's imports light

    if isinstance(e, (ReaderError, JevError)):
        return str(e).rstrip(".")
    if isinstance(e, anthropic.APIError):
        return friendly_error(e).rstrip(".")
    return "an internal error"
