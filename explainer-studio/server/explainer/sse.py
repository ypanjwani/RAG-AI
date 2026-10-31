"""Server-Sent Events: run work in the background and stream what it sends."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Iterator

from .js import json_stringify

log = logging.getLogger(__name__)

PING_SECONDS = 15  # comment pings keep proxies from closing the connection during long model calls
_DONE = object()

Send = Callable[[object], None]


def stream(work: Callable[[Send], None]) -> Iterator[str]:
    """Start work(send) in a thread and yield SSE lines.

    If the reader closes the page, the generator is closed and nothing more is written; the work
    itself runs to the end, as it did on Cloudflare."""
    q: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            work(q.put)
        except Exception:  # work should report its own errors; this is a last resort
            log.exception("stream work failed")
        finally:
            q.put(_DONE)

    threading.Thread(target=run, daemon=True).start()
    while True:
        try:
            item = q.get(timeout=PING_SECONDS)
        except queue.Empty:
            yield ": ping\n\n"
            continue
        if item is _DONE:
            return
        yield f"data: {json_stringify(item)}\n\n"
