"""TypeSafe Jev client: a "decision model" that answers typed questions about a state.

Docs: https://docs.typesafe.ai (POST /v1/systemone, Bearer auth). Jev only
answers choice and yes/no ("noul") questions; it can't write. Everything that
uses it must still work when JEV_API_KEY is missing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .js import is_num

log = logging.getLogger("explainer")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
# A versioned id, as the docs recommend: the "jev-latest" alias can move to a new model without notice
# (it pointed to jev-1.13.0 when this was pinned, Oct 2026).
JEV_MODEL = "jev-1.13.0"
TIMEOUT_S = 20.0
# Rate limited (429), overloaded (529) or unreachable: retry with backoff, as the docs advise.
RETRY_STATUS = (429, 529)
RETRY_WAITS = (0.5, 1.5)  # seconds before the 2nd and 3rd attempts; a Retry-After header wins, capped below
RETRY_AFTER_MAX = 5.0

# A question: {"type": "noul", "instructions", "criteria"?: {"true", "false"}}, {"type": "choice", "instructions", "criteria": {option: description}}
# or {"type": "score", "instructions", "criteria": [level description, low to high]}.
# An answer: {"type": "noul", "p"}, {"type": "choice", "ranking": [{"option", "p"}...], "confidence"}
# or {"type": "score", "score" (0..levels-1), "norm" (score scaled to 0..1), "confidence"}.


class JevError(Exception):
    """Jev failed. The message is fixed text written here (no exception details), so readers may see it."""


def parse_jev_answers(questions: dict[str, dict], raw: Any) -> dict[str, dict]:
    """Normalise a raw response against the questions asked: noul -> p, choice -> options by probability (unknown options dropped)."""
    answers = raw.get("answers") if isinstance(raw, dict) and isinstance(raw.get("answers"), dict) else {}
    out: dict[str, dict] = {}
    for qid, q in questions.items():
        a = answers.get(qid)
        if not a or not isinstance(a, dict):
            continue
        if q["type"] == "noul" and is_num(a.get("noul")):
            out[qid] = {"type": "noul", "p": a["noul"]}
        probs = a.get("probabilities")
        if q["type"] == "score" and is_num(a.get("score")):
            top = len(q["criteria"]) - 1
            out[qid] = {"type": "score", "score": a["score"], "norm": a["score"] / top if top > 0 else 0,
                        "confidence": a["confidence"] if is_num(a.get("confidence")) else 0}
        if q["type"] == "choice" and isinstance(probs, dict):
            ranking = [{"option": o, "p": p} for o, p in probs.items() if o in q["criteria"] and is_num(p)]
            ranking.sort(key=lambda x: -x["p"])  # stable, like Array.prototype.sort
            if ranking:
                out[qid] = {"type": "choice", "ranking": ranking, "confidence": a["confidence"] if is_num(a.get("confidence")) else 0}
    return out


def jev_ask(key: str, state: Any, questions: dict[str, dict]) -> dict:
    """Ask several questions about one state: {"answers", "model", "usage"}. Raises JevError with a readable message.
    Retries a rate-limited, overloaded or unreachable call up to len(RETRY_WAITS) times."""
    res = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            res = httpx.post(
                JEV_URL,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                json={"model": JEV_MODEL, "state": state, "questions": questions},
                timeout=TIMEOUT_S,
            )
        except httpx.HTTPError as e:
            if attempt == len(RETRY_WAITS):
                # The network error (host names, addresses) goes to the log only; readers see the reason (audit F06).
                log.warning("Jev unreachable: %s", e)
                raise JevError("Jev unreachable") from e
            time.sleep(RETRY_WAITS[attempt])
            continue
        if res.status_code not in RETRY_STATUS or attempt == len(RETRY_WAITS):
            break
        time.sleep(_retry_wait(res, RETRY_WAITS[attempt]))
    assert res is not None
    if res.status_code >= 400:
        reason = {401: "invalid key", 422: "request rejected", 429: "rate limited", 529: "overloaded"}.get(res.status_code, f"HTTP {res.status_code}")
        raise JevError(f"Jev {reason}")
    try:
        body = res.json()
    except ValueError as e:
        raise JevError("Jev sent an unreadable reply") from e
    usage = body.get("usage") if isinstance(body, dict) else None
    return {
        "answers": parse_jev_answers(questions, body),
        "model": (body.get("model") if isinstance(body, dict) else None) or "jev",
        "usage": usage if isinstance(usage, dict) else {"input_tokens": 0, "output_tokens": 0},
    }


def _retry_wait(res: httpx.Response, default: float) -> float:
    """Seconds to wait before retrying: the Retry-After header (seconds) if given, capped; else the default."""
    headers = getattr(res, "headers", None) or {}
    try:
        return min(max(float(headers.get("retry-after", "")), 0.0), RETRY_AFTER_MAX)
    except (TypeError, ValueError):
        return default
