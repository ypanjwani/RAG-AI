"""Pull the JSON object out of a model reply.

Stray code fences or prose around it are ignored; small syntax slips (an extra
closing brace, a missing comma, a reply cut off mid-array) are repaired so they
don't cost a full retry.
"""

from __future__ import annotations

import re
from typing import Any

import json_repair

from .js import json_parse


def _repair(s: str) -> Any:
    value = json_repair.loads(s)
    if not isinstance(value, (dict, list)):
        raise ValueError("not a JSON object")
    return value


def extract_json(text: str) -> tuple[Any, bool]:
    """(value, repaired). Raises ValueError when there is no usable JSON object."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    end = text.rfind("}")
    whole = text[start:end + 1] if end > start else ""
    if whole:
        try:
            return json_parse(whole), False
        except ValueError:
            pass
    # From the first "{" to the end, so a cut-off reply can still be closed;
    # then just up to the last "}", in case prose after the JSON confused it.
    for candidate in (re.sub(r"\s*```\s*\Z", "", text[start:]), whole):
        if not candidate:
            continue
        try:
            return _repair(candidate), True
        except Exception:
            continue
    raise ValueError("invalid JSON that could not be repaired")
