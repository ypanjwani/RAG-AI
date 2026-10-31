"""Port of shared/references.ts: cut a paper's reference list (and everything after it) before summarizing.

The PDF reader only finds headings by font size, which fails on some layouts
(two-column journals): then "References" is just a line in the text. So besides
a "References" section heading, the list's start is also found by content.
"""

from __future__ import annotations

import re

from ..js import WS, js_len, js_trim

_A = re.A
HEADING = re.compile(r"^" + WS + r"*(\d+\.?" + WS + r"*)?(references|bibliography|works cited)\b", re.I | _A)
TITLE_LINE = re.compile(r"^" + WS + r"*(\d+\.?" + WS + r"*)?(references|bibliography|works cited)" + WS + r"*\Z", re.I | _A)
YEAR = re.compile(r"\b(19|20)\d{2}[a-z]?\b", _A)
# Dotted abbreviations get no trailing \b: "vol." is followed by a space, not a word character.
VENUE = re.compile(r"\b(arXiv|Proceedings|Conference|Journal|Transactions|PMLR|NeurIPS|ICML|ICLR|CVPR|ICCV|ECCV|ACL|EMNLP|NAACL|AAAI|IJCAI|preprint|IEEE|ACM|Springer|Elsevier)\b|\b(pp|vol|Proc|doi|no)\.|\bIn Advances", _A)
AUTHOR = re.compile(r"([A-Z][a-z]+," + WS + r"+[A-Z][a-z]*\.?|\b[A-Z]\." + WS + r"?[A-Z][a-z]+|\bet al\.)", _A)
ENTRY = re.compile(r"^" + WS + r"*(\[\d{1,3}\]|\d{1,3}\.?" + WS + r"+[A-Z][a-z]+,?" + WS + r")", _A)

START_SHARE = 0.4
WINDOW, MIN_WINDOW, MIN_SHARE, MIN_LINE = 12, 6, 0.5, 20


def is_reference_line(line: str) -> bool:
    return bool(ENTRY.search(line)) or (bool(YEAR.search(line)) and (bool(VENUE.search(line)) or bool(AUTHOR.search(line)))) or (bool(VENUE.search(line)) and bool(AUTHOR.search(line)))


def cut_references(sections: list[dict]) -> dict:
    """{"sections", "removed" (UTF-16 chars), "by"?}."""
    total = sum(js_len(s["text"]) for s in sections)

    def result(keep: list[dict], by: str) -> dict:
        return {"sections": keep, "removed": total - sum(js_len(s["text"]) for s in keep), "by": by}

    # 1. A real "References" section heading (big font): trust it.
    for i, s in enumerate(sections):
        if i > 2 and HEADING.search(s["heading"]):
            return result(sections[:i], "heading")

    # 2. The line where the list starts. A lone "References" line counts only if entries follow.
    lines: list[dict] = []
    pos = 0
    for si, s in enumerate(sections):
        off = 0  # Python index into the section text
        uoff = 0  # the same offset in UTF-16 units, for the START_SHARE test
        for text in s["text"].split("\n"):
            lines.append({"si": si, "off": off, "pos": pos + uoff, "text": text, "long": js_len(js_trim(text)) >= MIN_LINE})
            off += len(text) + 1
            uoff += js_len(text) + 1
        pos += js_len(s["text"])

    def entries_follow(start: int) -> bool:
        win = []
        i = start
        while i < len(lines) and len(win) < WINDOW:
            if lines[i]["long"]:
                win.append(lines[i])
            i += 1
        return len(win) >= MIN_WINDOW and sum(1 for w in win if is_reference_line(w["text"])) / len(win) >= MIN_SHARE

    for i, ln in enumerate(lines):
        if ln["pos"] < total * START_SHARE:
            continue
        if TITLE_LINE.search(ln["text"]) and entries_follow(i + 1):
            by = "title line"
        elif ln["long"] and is_reference_line(ln["text"]) and entries_follow(i):
            by = "content"
        else:
            continue
        head = js_trim(sections[ln["si"]]["text"][: ln["off"]])
        keep = [*sections[: ln["si"]], *([{**sections[ln["si"]], "text": head}] if head else [])]
        return result(keep, by)
    return {"sections": sections, "removed": 0}
