"""Paper cache: the expensive first steps of a paper's generation (Jev's section filter and Haiku's section
summaries), stored under a hash of the paper's text so the same PDF is never read twice.

The key covers everything those steps read (the title and the sections after the reference cut) plus
CACHE_VERSION: bump it when the summarize prompt, the filter or the grouping changes, and old entries are
simply never hit again (prune() drops them after MAX_AGE_DAYS unused).

Shared by every user: an entry can only be hit by someone who sends the exact same text, and it holds
nothing beyond what that text already says. The raw PDF never reaches the server (the browser extracts
the text), so there is nothing else to store.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import delete, update

from .db import Database, PaperCache, now_ms
from .js import json_stringify

log = logging.getLogger("explainer")

CACHE_VERSION = "1"
MAX_AGE_DAYS = 30


def paper_key(title: str, sections: list[dict]) -> str:
    canonical = json_stringify({"v": CACHE_VERSION, "title": title, "sections": [[s["heading"], s["text"]] for s in sections]})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PaperStore:
    """What pipeline.generate() calls (get/put); errors are logged, never raised: a cache miss is always fine."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            with self.db.session() as s:
                row = s.get(PaperCache, key)
                if row is None:
                    return None
                s.execute(update(PaperCache).where(PaperCache.key == key).values(hits=PaperCache.hits + 1, used_at=now_ms()))
                return row.value
        except Exception:
            log.exception("paper cache read failed")
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        try:
            with self.db.session() as s:
                row = s.get(PaperCache, key)
                if row is None:
                    s.add(PaperCache(key=key, value=value))
                else:
                    row.value, row.used_at = value, now_ms()
        except Exception:  # e.g. two generations of the same paper finishing together
            log.exception("paper cache write failed")

    def prune(self) -> int:
        with self.db.session() as s:
            return s.execute(delete(PaperCache).where(PaperCache.used_at < now_ms() - MAX_AGE_DAYS * 86_400_000)).rowcount
