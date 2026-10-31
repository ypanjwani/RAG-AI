"""Records the token usage of every Claude call in one generation and prices it.

USD per million tokens; update when prices change. Cache writes are priced at
1.25x input (5-minute cache), cache reads as listed.
"""

from __future__ import annotations

import threading
import time
from typing import Any

PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5-5": {"input": 2, "output": 10, "cacheRead": 0.2, "cacheWrite": 2.5},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125},
    # TypeSafe Jev (decision model): input only, output is free. Versioned ids like "jev-1.13.0".
    "jev": {"input": 0.042, "output": 0, "cacheRead": 0, "cacheWrite": 0},
}
# Unknown models are priced like Sonnet, the most expensive model listed, so costs are never understated.
FALLBACK_PRICE = PRICES["claude-sonnet-5-5"]


def usage_dict(u: Any) -> dict[str, int]:
    """An SDK Usage object or a plain dict, as {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}."""
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    return {
        "input_tokens": get("input_tokens") or 0,
        "output_tokens": get("output_tokens") or 0,
        "cache_read_input_tokens": get("cache_read_input_tokens") or 0,
        "cache_creation_input_tokens": get("cache_creation_input_tokens") or 0,
    }


def price_of(model: str, u: Any) -> float:
    p = PRICES.get(model) or (PRICES["jev"] if model.startswith("jev") else FALLBACK_PRICE)
    d = usage_dict(u)
    return (d["input_tokens"] * p["input"] + d["output_tokens"] * p["output"]
            + d["cache_read_input_tokens"] * p["cacheRead"] + d["cache_creation_input_tokens"] * p["cacheWrite"]) / 1e6


class UsageLog:
    """Thread-safe: parallel calls (summaries, widgets, fact-check chunks) log into the same generation."""

    def __init__(self, quality: str, effort: str = "medium"):
        self.quality = quality
        self.effort = effort
        self._steps: list[dict] = []
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self.tag = ""  # appended to every step name while set, e.g. "round 2" (Quality 2.0, deepen.py)

    def label(self, quality: str, effort: str) -> None:
        """Name the mode and effort of a log made before they were known (the /api/generate route's). A labeled log keeps its own."""
        if not self.quality:
            self.quality, self.effort = quality, effort

    def add(self, step: str, model: str, usage: Any) -> None:
        d = usage_dict(usage)
        with self._lock:
            self._steps.append({
                "step": f"{step} ({self.tag})" if self.tag else step, "model": model, "input": d["input_tokens"], "output": d["output_tokens"],
                "cacheRead": d["cache_read_input_tokens"], "cacheWrite": d["cache_creation_input_tokens"], "usd": price_of(model, d),
            })

    def add_message(self, step: str, msg: Any) -> None:
        """Log an SDK Message (its model and usage)."""
        self.add(step, msg.model, msg.usage)

    def summary(self) -> dict:
        with self._lock:
            steps = list(self._steps)
        total = 0.0
        for s in steps:
            total += s["usd"]
        return {"quality": self.quality, "effort": self.effort, "steps": steps, "totalUsd": total, "seconds": round(time.monotonic() - self._start)}
