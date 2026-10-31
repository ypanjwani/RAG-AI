"""Smart mode test: can Jev pick each section's writing style and visual? (Haiku is run on the same input to compare.)

From server/, with the venv:
    .venv/Scripts/python -m evals.smart_route --dry-run         # build the inputs, no API calls
    .venv/Scripts/python -m evals.smart_route --pages 2         # first 2 pages only
    .venv/Scripts/python -m evals.smart_route --haiku [--full]  # all pages, Jev and Haiku; --full sends the whole prose

Labels: evals/smart_labels.json (edit them). Pages: ../.variety/<page>.json. Report: ../.variety/smart-eval/<time>.json.
Jev sees what a plan would hold (title, every section's label, this section's label and blurb, the start of its
prose), never the finished page's block types, which would give the answer away.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from explainer.clarity import _prose, _reader
from explainer.jev import JevError, jev_ask
from explainer.pricing import UsageLog
from explainer.smart import TEXT, VISUAL, questions, section_state

ROOT = Path(__file__).resolve().parents[2]  # explainer-studio/
LABELS = Path(__file__).with_name("smart_labels.json")
VARIETY = ROOT / ".variety"
HEAD_CHARS, FULL_CHARS = 300, 2500
CONFIDENT = 0.45  # the page brief's threshold for trusting a Jev choice

# The criteria and the state shape are the product's own (explainer/smart.py), so this test measures what Smart mode runs.


def load_page(name: str) -> dict:
    d = json.loads((VARIETY / f"{name}.json").read_text(encoding="utf-8"))
    return d.get("spec", d)


def build_cases(labels: list[dict], full: bool, pages: int | None) -> list[dict]:
    order = list(dict.fromkeys(x["page"] for x in labels))[:pages]
    specs = {p: load_page(p) for p in order}
    cases = []
    for x in labels:
        if x["page"] not in specs:
            continue
        spec = specs[x["page"]]
        idx = next((i for i, st in enumerate(spec["stages"]) if st["id"] == x["stage_id"]), None)
        if idx is None:
            sys.exit(f"{x['page']}: no stage {x['stage_id']}")
        st = spec["stages"][idx]
        state = section_state(spec["title"], _reader(spec), [s["label"] for s in spec["stages"]], idx, st["label"], st.get("blurb", ""),
                              _prose(st, FULL_CHARS if full else HEAD_CHARS))
        cases.append({**x, "state": state})
    return cases


def run_jev(key: str, cases: list[dict], usage: UsageLog) -> None:
    def ask(c: dict) -> dict:
        try:
            return jev_ask(key, c["state"], questions())
        except JevError as e:
            return {"error": str(e)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(ask, cases))
    total, model = 0, "jev"
    for c, r in zip(cases, results):
        if "error" in r:
            c["jev"] = {"error": r["error"]}
            continue
        total += r["usage"].get("input_tokens", 0)
        model = r["model"]
        c["jev"] = {q: {"pick": a["ranking"][0]["option"], "p": round(a["ranking"][0]["p"], 3),
                        "ranking": [[x["option"], round(x["p"], 3)] for x in a["ranking"]]}
                    for q, a in r["answers"].items()}
    usage.add("jev", model, {"input_tokens": total, "output_tokens": 0})


HAIKU_SYSTEM = "\n".join([
    "You decide, for each section of an explainer page that is about to be written, how its text is written and what visual it gets.",
    "Text style (pick one):",
    *[f"- {k}: {v}" for k, v in TEXT.items()],
    "Visual (pick one):",
    *[f"- {k}: {v}" for k, v in VISUAL.items()],
    "`section` means the section being judged. Judge each section on its own merits; a page usually mixes several styles.",
])


def run_haiku(cases: list[dict], usage: UsageLog) -> None:
    import anthropic
    from explainer.llm import write_text

    client = anthropic.Anthropic()
    fmt = {
        "type": "object", "additionalProperties": False, "required": ["sections"],
        "properties": {"sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text", "visual"],
            "properties": {"id": {"type": "string"}, "text": {"type": "string", "enum": list(TEXT)}, "visual": {"type": "string", "enum": list(VISUAL)}},
        }}},
    }
    by_page: dict[str, list[dict]] = {}
    for c in cases:
        by_page.setdefault(c["page"], []).append(c)

    def page(items: list[dict]) -> None:
        e = items[0]["state"]["explainer"]
        body = [f"Explainer: {e['title']} (reader: {e['reader']})", "Outline: " + " | ".join(items[0]["state"]["outline"])]
        for c in items:
            s = c["state"]["section"]
            body.append(f"[{c['stage_id']}] {s['title']} ({s['position']})\nSummary: {s['summary']}\nContent: {s['content']}")
        try:
            out = json.loads(write_text(client, usage, "haiku", system=HAIKU_SYSTEM, user="\n\n".join(body), max_tokens=2000, fmt=fmt))
        except Exception as ex:  # report and go on: one failed page shouldn't end the run
            for c in items:
                c["haiku"] = {"error": str(ex)}
            return
        got = {r["id"]: r for r in out["sections"]}
        for c in items:
            r = got.get(c["stage_id"])
            c["haiku"] = {"text": {"pick": r["text"]}, "visual": {"pick": r["visual"]}} if r else {"error": "missing"}

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(page, by_page.values()))


def report(cases: list[dict], who: str) -> dict:
    out: dict = {}
    for q, options in (("text", list(TEXT)), ("visual", list(VISUAL))):
        rows = [c for c in cases if q in c.get(who, {})]
        if not rows:
            continue
        hit = sum(c[who][q]["pick"] == c[q] for c in rows)
        confusion = {want: dict(Counter(c[who][q]["pick"] for c in rows if c[q] == want)) for want in options}
        res = {"n": len(rows), "agree": hit, "agree_pct": round(100 * hit / len(rows)),
               "labels": dict(Counter(c[q] for c in rows)), "picks": dict(Counter(c[who][q]["pick"] for c in rows)),
               "confusion": confusion}
        if who == "jev":
            sure = [c for c in rows if c[who][q]["p"] >= CONFIDENT]
            res["confident"] = len(sure)
            res["confident_agree_pct"] = round(100 * sum(c[who][q]["pick"] == c[q] for c in sure) / len(sure)) if sure else None
            res["fallback"] = len(rows) - len(sure)
        out[q] = res
    return out


def show(name: str, rep: dict) -> None:
    for q, r in rep.items():
        line = f"{name:6} {q:7} agree {r['agree']}/{r['n']} ({r['agree_pct']}%)  picks {r['picks']}"
        if "confident" in r:
            line += f"  | confident {r['confident']} -> {r['confident_agree_pct']}% agree, {r['fallback']} fall back"
        print(line)
        for want, got in r["confusion"].items():
            print(f"         label {want:11} -> {got}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="send the whole prose, not just its start")
    ap.add_argument("--haiku", action="store_true", help="also ask Haiku")
    ap.add_argument("--pages", type=int, help="only the first N pages")
    ap.add_argument("--dry-run", action="store_true", help="build the inputs, no API calls")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    labels = json.loads(LABELS.read_text(encoding="utf-8"))["sections"]
    cases = build_cases(labels, args.full, args.pages)
    print(f"{len(cases)} sections from {len({c['page'] for c in cases})} pages ({'full prose' if args.full else 'plan view'})")
    if args.dry_run:
        print(json.dumps(cases[0]["state"], indent=2, ensure_ascii=False))
        return

    key = os.environ.get("JEV_API_KEY")
    if not key:
        sys.exit("JEV_API_KEY is not set (explainer-studio/.env).")
    usage = UsageLog("smart-eval", "low")
    run_jev(key, cases, usage)
    if args.haiku:
        run_haiku(cases, usage)

    errors = [(c["stage_id"], d[w]["error"]) for c in cases for w, d in (("jev", c), ("haiku", c)) if w in d and "error" in d[w]]
    for sid, err in errors:
        print(f"error {sid}: {err}")
    reports = {w: report(cases, w) for w in ("jev", "haiku") if any(w in c for c in cases)}
    for w, rep in reports.items():
        show(w, rep)
    cost = usage.summary()
    print(f"cost ${cost['totalUsd']:.4f} in {cost['seconds']} s")

    out = VARIETY / "smart-eval" / f"{time.strftime('%Y%m%d-%H%M%S')}{'-full' if args.full else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"args": vars(args), "reports": reports, "usage": cost,
                               "cases": [{k: v for k, v in c.items() if k != "state"} for c in cases]},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
