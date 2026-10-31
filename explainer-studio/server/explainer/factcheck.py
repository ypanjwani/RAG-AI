"""Fact check (Quality+ and Freeform+, after the page is built): Jev flags statements that look wrong, Haiku
reviews only those and fixes the ones that really are. Never raises: without a Jev key, or on
any failure, the page is returned as it was with a warning.

Jev is used as its docs recommend: the statements and the page's computed values
are in the state, and each yes/no question points at one statement by path, with
criteria, asking one thing (factually correct? consistent with the page values?).
The two answers are combined in code. Measured on 388 claims (Oct 2026): AUC 0.98.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import anthropic

from . import data
from .core.factcheck import apply_fixes, claims_of, page_values
from .core.validate import check_edit
from .jev import jev_ask
from .js import js_slice, json_parse
from .llm import warning_reason, write_text
from .pricing import UsageLog

log = logging.getLogger(__name__)

FLAG_BELOW = 0.85  # flag a claim when the lower of its two Jev answers is below this
BATCH = 20
REVIEW_CHUNK = 8  # flagged claims per Haiku review call

FACT = {
    "true": "Everything the statement says about AI, machine learning, maths or the cited research is accurate. Simplifications for beginners are fine.",
    "false": "The statement contains a factual error: a wrong direction (increase vs decrease), a wrong number or formula, a wrong attribution, or a quiz whose marked answer is not the correct one.",
}
CONSISTENT = {
    "true": "The statement agrees with every value in `page_values` it refers to, or refers to none of them.",
    "false": "The statement contradicts a value in `page_values` (for example a quiz answer or number that differs from what the page computes).",
}
# Evals only (section_check, FACTCHECK_SECTIONS=1): a third question, against the rest of the statement's own section.
IN_SECTION = {
    "true": "The statement agrees with everything else its section says in `sections`.",
    "false": "The statement contradicts something else its own section says in `sections` (a different number, direction or definition for the same thing).",
}


def score(jev_key: str, spec: dict, claims: list[dict], values: list[str], usage: UsageLog, section_check: bool = False) -> dict[str, float]:
    """Jev's score per claim id (the lower of "factually correct" and "consistent with the page values",
    and with section_check also "consistent with its own section")."""
    batches = [claims[i:i + BATCH] for i in range(0, len(claims), BATCH)]

    def ask(batch: list[dict]) -> dict:
        state = {
            "explainer": {"title": spec["title"], "level": spec["level"]},
            "page_values": values,
            "statements": {f"s{k}": {"section": spec["stages"][c["stage"]]["label"], "text": c["text"]} for k, c in enumerate(batch)},
        }
        if section_check:
            state["sections"] = {spec["stages"][s]["label"]: _stage_text(spec, s) for s in dict.fromkeys(c["stage"] for c in batch)}
        questions: dict[str, dict] = {}
        for k in range(len(batch)):
            questions[f"f{k}"] = {"type": "noul", "instructions": f"Is `statements.s{k}.text` factually correct?", "criteria": FACT}
            questions[f"c{k}"] = {"type": "noul", "instructions": f"Is `statements.s{k}.text` consistent with `page_values`?", "criteria": CONSISTENT}
            if section_check:
                questions[f"x{k}"] = {"type": "noul", "instructions": f"Is `statements.s{k}.text` consistent with the rest of its section in `sections`?", "criteria": IN_SECTION}
        return jev_ask(jev_key, state, questions)

    with ThreadPoolExecutor(max_workers=max(1, len(batches))) as pool:
        results = list(pool.map(ask, batches))
    scores: dict[str, float] = {}
    total_input, model = 0, "jev"
    for batch, r in zip(batches, results):
        total_input += r["usage"].get("input_tokens", 0)
        model = r["model"]
        for k, c in enumerate(batch):
            f, co = r["answers"].get(f"f{k}"), r["answers"].get(f"c{k}")
            if f and co and f["type"] == "noul" and co["type"] == "noul":
                x = r["answers"].get(f"x{k}")
                scores[c["id"]] = min(f["p"], co["p"], x["p"] if x and x["type"] == "noul" else 1)
    usage.add("jev factcheck", model, {"input_tokens": total_input, "output_tokens": 0})
    return scores


def _stage_text(spec: dict, stage: int) -> str:
    """The stage's own prose, so Haiku judges a sentence in context."""
    st = spec["stages"][stage]
    parts = [b["md"] for b in st["blocks"] if b["type"] in ("text", "callout")]
    return js_slice(f"{st['label']}: {' '.join(parts)}", 0, 1500)


def review(client: anthropic.Anthropic, spec: dict, flagged: list[dict], values: list[str], usage: UsageLog, effort: str = "low") -> list[dict]:
    """Haiku's verdict on each flagged claim, as fixes (claims judged correct are left out)."""
    stages = list(dict.fromkeys(c["stage"] for c in flagged))
    user = "\n\n".join([
        f"Explainer: {spec['title']} (level: {spec['level']})",
        "Page values (computed by code at the starting settings):\n" + ("\n".join(values) or "(none)"),
        "Context, the flagged stages' text:\n" + "\n\n".join(_stage_text(spec, s) for s in stages),
        "Flagged statements:\n" + "\n\n".join(f'[{c["id"]}] (stage "{spec["stages"][c["stage"]]["label"]}")\n{c["text"]}' for c in flagged),
    ])
    text = write_text(client, usage, "fact check", system=data.prompt("FACTCHECK_SYSTEM"), user=user, max_tokens=8000,
                      fmt=data.formats()["repair"], effort=effort)
    results = json_parse(text)["results"]
    fixes = []
    for r in results:
        if r.get("verdict") != "fix":
            continue
        fix = {"id": r["id"], "edits": r.get("edits") or []}
        if isinstance(r.get("answer"), (int, float)) and r["answer"] >= 0:
            fix["answer"] = r["answer"]
        fixes.append(fix)
    return fixes


def fact_check(client: anthropic.Anthropic, jev_key: str | None, spec: dict, usage: UsageLog, section_check: bool = False,
               only: list[int] | None = None) -> tuple[dict, str]:
    """Check a finished page (or only the stages at these indices: a revision's changed stages) and fix what is
    wrong: (spec, progress detail). spec["factCheck"] says how many claims were checked, flagged and fixed; each
    fix becomes a warning showing the before and after. page_values always covers the whole page."""
    if not jev_key:
        return {**spec, "warnings": [*spec["warnings"], "Fact check skipped: no Jev key."]}, "skipped (no Jev key)"
    claims = claims_of(spec)
    if only is not None:
        claims = [c for c in claims if c["stage"] in set(only)]
    if not claims:
        return spec, "nothing to check"
    values = page_values(spec)
    try:
        scores = score(jev_key, spec, claims, values, usage, section_check)
        flagged = [c for c in claims if scores.get(c["id"], 1) < FLAG_BELOW]
        stats = {"checked": len(claims), "flagged": len(flagged), "fixed": 0}
        if not flagged:
            return {**spec, "factCheck": stats}, f"{len(claims)} statements, none flagged"

        # Haiku misses errors when one call reviews ~30 claims, so it reviews a few at a time, in parallel.
        chunks = [flagged[i:i + REVIEW_CHUNK] for i in range(0, len(flagged), REVIEW_CHUNK)]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            fixes = [f for fs in pool.map(lambda ch: review(client, spec, ch, values, usage), chunks) for f in fs]
        r = apply_fixes(spec, claims, fixes)
        # Text and quiz answers can't break a chart, but run the page checker on every changed stage anyway.
        nxt = r["spec"]
        stage_of = {c["id"]: c["stage"] for c in claims}
        reverted: set[int] = set()
        for s in dict.fromkeys(stage_of[a["id"]] for a in r["applied"]):
            if check_edit(nxt, s, nxt["stages"][s]["blocks"])["ok"]:
                continue
            reverted.add(s)
            nxt = {**nxt, "stages": [spec["stages"][s] if i == s else st for i, st in enumerate(nxt["stages"])]}
        applied = [a for a in r["applied"] if stage_of[a["id"]] not in reverted]
        stats["fixed"] = len({a["id"] for a in applied})
        for s in r["skipped"]:
            log.warning("fact check: fix %s not applied (%s)", s["id"], s["why"])
        notes = [f'Fact check ({a["where"]}): "{a["from"]}" → "{a["to"]}"' for a in applied]
        return ({**nxt, "factCheck": stats, "warnings": [*nxt["warnings"], *notes]},
                f"{len(claims)} statements · Jev flagged {len(flagged)} · fixed {stats['fixed']}")
    except Exception as e:
        log.exception("fact check failed")
        return {**spec, "warnings": [*spec["warnings"], f"Fact check skipped: {warning_reason(e)}."]}, "skipped (error)"
