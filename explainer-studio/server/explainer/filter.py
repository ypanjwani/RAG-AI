"""PDF section filter: before a paper is summarized, Jev judges each section (the PDF reader
splits papers by heading, not by page) and side material is skipped, so fewer chunks reach
the Haiku summarizer. It pays off mainly on long papers."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .jev import jev_ask
from .js import js_len, js_slice
from .pricing import UsageLog

log = logging.getLogger(__name__)

# A choice between two described options separates far better than yes/no.
QUESTION = "How much does this section of a research paper matter for explaining the paper to a learner?"
CRITERIA = {
    "core": "Explains the problem, the method, its equations, the experimental setup, or the main results and limitations: needed to explain the paper.",
    "side": "Not needed to explain the paper: acknowledgements, author, funding or ethics statements, reproducibility checklists, long related-work surveys, extra proofs, or repeated tables and appendix details.",
}
EXCERPT_CHARS = 3000  # Jev sees the start of each section: enough to judge it, cheap to send
CONCURRENCY = 6
THRESHOLD = 0.4  # keep a section when Jev's probability of "core" is at least this
ALWAYS_KEEP = 2  # the first sections (abstract, introduction) are always kept
MIN_KEEP_SHARE = 0.4  # never skip more than 60% of the paper's text


def choose_sections(scores: list[dict]) -> set[int]:
    """scores: {"index", "p" (None = couldn't be judged), "chars"}. The first ALWAYS_KEEP, every unjudged one and
    every one scoring at least THRESHOLD; then the best-scoring skipped ones until MIN_KEEP_SHARE of the text is kept."""
    keep = {s["index"] for s in scores if s["index"] < ALWAYS_KEEP or s["p"] is None or s["p"] >= THRESHOLD}
    total = sum(s["chars"] for s in scores)
    kept = sum(s["chars"] for s in scores if s["index"] in keep)
    rest = sorted((s for s in scores if s["index"] not in keep), key=lambda s: -(1 if s["p"] is None else s["p"]))
    for s in rest:
        if kept >= total * MIN_KEEP_SHARE:
            break
        keep.add(s["index"])
        kept += s["chars"]
    return keep


def filter_sections(jev_key: str | None, title: str, sections: list[dict], usage: UsageLog) -> dict:
    """{"sections", "skipped" (headings), "note"?}. Without Jev (or if it fails) sections are kept and the note says why."""
    if not jev_key:
        return {"sections": sections, "skipped": [], "note": "no Jev key, so every section was kept"}
    model = ["jev"]
    totals = {"input": 0, "output": 0, "unjudged": 0}
    lock = threading.Lock()

    def count(key: str, n: int = 1) -> None:
        with lock:
            totals[key] += n

    def judge(item: tuple[int, dict]) -> dict:
        index, s = item
        try:
            r = jev_ask(jev_key, {"paper": title, "section": s["heading"], "excerpt": js_slice(s["text"], 0, EXCERPT_CHARS)},
                        {"importance": {"type": "choice", "instructions": QUESTION, "criteria": CRITERIA}})
            model[0] = r["model"]
            count("input", r["usage"].get("input_tokens", 0))
            count("output", r["usage"].get("output_tokens", 0))
            a = r["answers"].get("importance")
            if not a or a["type"] != "choice":
                count("unjudged")
                return {"index": index, "p": None, "chars": js_len(s["text"])}
            core = next((x["p"] for x in a["ranking"] if x["option"] == "core"), 0)
            return {"index": index, "p": core, "chars": js_len(s["text"])}
        except Exception as e:
            log.error("jev filter failed for %r: %s", s["heading"], e)
            count("unjudged")
            return {"index": index, "p": None, "chars": js_len(s["text"])}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        scores = list(pool.map(judge, enumerate(sections)))
    if totals["input"] or totals["output"]:
        usage.add("jev filter", model[0], {"input_tokens": totals["input"], "output_tokens": totals["output"]})
    keep = choose_sections(scores)
    out = {
        "sections": [s for i, s in enumerate(sections) if i in keep],
        "skipped": [s["heading"] for i, s in enumerate(sections) if i not in keep],
    }
    if totals["unjudged"]:
        out["note"] = f"{totals['unjudged']} section(s) couldn't be judged and were kept"
    return out
