"""Chart block test: does the planner use the chart block well (CHART_BLOCK on vs off), and does Jev pick views better?

From server/, with the venv:
    .venv/Scripts/python -X utf8 -m evals.chart_eval generate          # 8 topics, switch off and on (real API calls, ~1 cent each)
    .venv/Scripts/python -X utf8 -m evals.chart_eval generate --only on --topics 1 2
    .venv/Scripts/python -X utf8 -m evals.chart_eval report            # costs, block counts, every chart and random block
    .venv/Scripts/python -X utf8 -m evals.chart_eval jev               # Jev's view pick vs the planner's, against evals/chart_labels.json

Pages: ../.variety/chart/<off|on>-<n>.json (git-ignored; they also feed `npm run parity`).
Labels: evals/chart_labels.json, written by a person reading each chart: {"on-3:stage-id:2": {"best": "compare", "note": "..."}}.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # explainer-studio/
OUT = ROOT / ".variety" / "chart"
LABELS = Path(__file__).with_name("chart_labels.json")

TOPICS = [
    "Can you explain bagging vs boosting in trees.",
    "How does dropout regularization work in neural networks?",
    "How does softmax temperature change what a language model samples?",
    "Explain the bias-variance trade-off.",
    "How does mini-batch size affect the noise in stochastic gradient descent?",
    "How does the rank in LoRA fine-tuning trade quality for parameters?",
    "How does k-means clustering work?",
    "Why does attention divide by the square root of d in scaled dot-product attention?",
]
RANDOM_TYPES = ("experiment", "samples")
COUNTED = ("chart", "plot", "samples", "heatmap", "experiment", "trajectory", "bars")

# What Jev reads for each view: the product's own text (explainer/chartpick.py), so this test measures what runs.
from explainer.chartpick import VIEW_CRITERIA  # noqa: E402


def page_path(mode: str, n: int) -> Path:
    return OUT / f"{mode}-{n}.json"


def generate(modes: list[str], topics: list[int]) -> None:
    import anthropic

    from explainer.pipeline import generate as run
    from explainer.requests import parse_generate

    OUT.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic()
    for n in topics:
        for mode in modes:
            p = page_path(mode, n)
            if p.exists():
                print(f"skip {p.name} (exists)")
                continue
            os.environ["CHART_BLOCK"] = "1" if mode == "on" else "0"
            req = parse_generate({"mode": "concept", "text": TOPICS[n - 1], "quality": "quality-plus", "effort": "low", "extras": ["auto"]})
            t0 = time.time()
            try:
                r = run(client, req, lambda e: None, jev_key=os.environ.get("JEV_API_KEY"), plan_effort="low")
            except Exception as e:  # keep going: one failed page shouldn't stop the batch
                print(f"FAILED {mode}-{n}: {e}")
                continue
            r["seconds"] = round(time.time() - t0)
            r["topic"] = TOPICS[n - 1]
            p.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{mode}-{n}: ${(r.get('usage') or {}).get('totalUsd', 0):.4f} {r['seconds']}s {r['spec']['title']}", flush=True)


def load(mode: str, n: int) -> dict | None:
    p = page_path(mode, n)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def blocks_of(spec: dict):
    for st in spec["stages"]:
        for i, b in enumerate(st["blocks"]):
            yield st, i, b


def report() -> None:
    totals = {m: {"pages": 0, "usd": 0.0, "seconds": 0, "dropped": 0, "counts": dict.fromkeys(COUNTED, 0)} for m in ("off", "on")}
    for n in range(1, len(TOPICS) + 1):
        for mode in ("off", "on"):
            r = load(mode, n)
            if not r:
                continue
            spec, t = r["spec"], totals[mode]
            usd = (r.get("usage") or {}).get("totalUsd", 0)
            t["pages"] += 1
            t["usd"] += usd
            t["seconds"] += r.get("seconds", 0)
            dropped = [w for w in spec.get("warnings", []) if w.startswith("Dropped")]
            t["dropped"] += len(dropped)
            counts = dict.fromkeys(COUNTED, 0)
            for _, _, b in blocks_of(spec):
                if b["type"] in counts:
                    counts[b["type"]] += 1
                    t["counts"][b["type"]] += 1
            print(f"\n=== {mode}-{n}: {spec['title']} | ${usd:.4f} | {r.get('seconds', '?')}s | {', '.join(f'{k} {v}' for k, v in counts.items() if v)}")
            for w in spec.get("warnings", []):
                if w.startswith("Dropped") or "chart" in w.lower():
                    print(f"  ! {w}")
            for st, i, b in blocks_of(spec):
                key = f"{mode}-{n}:{st['id']}:{i}"
                if b["type"] == "chart":
                    print(f"  [chart] {key} '{b['title']}' view={b['view']} x={b['x'] or '-'} by={b['by'] or '-'} at={b['at']}")
                    print(f"      lesson: {b['lesson']}")
                    print(f"      series: {' ; '.join(s['expr'] for s in b['series'])}")
                elif b["type"] in RANDOM_TYPES:
                    formula = b.get("trial") or b.get("values")
                    defs = "; ".join(f"{d['name']}={d['expr']}" for d in b.get("defs") or [])
                    print(f"  [{b['type']}] {key} '{b.get('title', '')}': {formula}" + (f"  (defs {defs})" if defs else ""))
    print("\n=== totals")
    for mode, t in totals.items():
        if t["pages"]:
            print(f"{mode}: {t['pages']} pages, ${t['usd'] / t['pages']:.4f}/page, {t['seconds'] / t['pages']:.0f}s/page, "
                  f"{t['dropped']} dropped-block warnings, blocks {t['counts']}")


def chart_cases() -> list[dict]:
    """Every chart on the 'on' pages, with the views that fit it (the same rules as the validator)."""
    from explainer.core.expr import eval_value, is_num, uses_random
    from explainer.core.validate import VIEWS_FOR, _view_fits, probe_scopes, slider_map
    from explainer.core.views import with_defs

    cases = []
    for n in range(1, len(TOPICS) + 1):
        r = load("on", n)
        if not r:
            continue
        spec = r["spec"]
        every = [b for _, _, b in blocks_of(spec)]
        scopes, sliders = probe_scopes(every), slider_map(every)
        for st, i, b in blocks_of(spec):
            if b["type"] != "chart":
                continue
            first = b["series"][0]["expr"]
            shape = "random" if uses_random(first, b.get("defs") or []) else ("number" if is_num(eval_value(first, with_defs(b.get("defs"), scopes[0]))) else "list")
            fits = [v for v in VIEWS_FOR[shape] if _view_fits(v, shape, b["x"], b["by"])]
            cases.append({"key": f"on-{n}:{st['id']}:{i}", "page": spec["title"], "chart": b, "fits": fits,
                          "x": sliders.get(b["x"], {}).get("label", ""), "by": sliders.get(b["by"], {}).get("label", "")})
    return cases


def jev() -> None:
    from explainer.jev import JevError, jev_ask

    labels = json.loads(LABELS.read_text(encoding="utf-8")) if LABELS.exists() else {}
    key = os.environ.get("JEV_API_KEY")
    rows = []
    for c in chart_cases():
        b = c["chart"]
        best = (labels.get(c["key"]) or {}).get("best")
        if len(c["fits"]) < 2:
            rows.append((c["key"], b["view"], b["view"], best, "only one view fits"))
            continue
        state = {"page": c["page"], "chart": {"title": b["title"], "lesson": b["lesson"], "formula": [s["label"] + ": " + s["expr"] for s in b["series"]],
                                               "x_axis_variable": c["x"] or "none", "second_variable": c["by"] or "none"}}
        q = {"view": {"type": "choice", "instructions": "Which view of `chart` shows its `lesson` best?", "criteria": {v: VIEW_CRITERIA[v] for v in c["fits"]}}}
        try:
            a = jev_ask(key, state, q)["answers"]["view"]["ranking"]
            pick, p = a[0]["option"], a[0]["p"]
            rows.append((c["key"], b["view"], pick, best, f"p={p:.2f}"))
        except (JevError, KeyError, IndexError) as e:
            rows.append((c["key"], b["view"], "?", best, f"error {e}"))
    right = {"planner": 0, "jev": 0}
    labelled = 0
    for key_, planner, pick, best, note in rows:
        print(f"{key_:40} planner={planner:9} jev={pick:9} label={best or '-':9} {note}")
        if best:
            labelled += 1
            right["planner"] += planner == best
            right["jev"] += pick == best
    print(f"\n{labelled} labelled charts: planner right {right['planner']}, Jev right {right['jev']}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["generate", "report", "jev"])
    ap.add_argument("--only", choices=["off", "on"])
    ap.add_argument("--topics", type=int, nargs="*")
    a = ap.parse_args()
    if a.cmd == "generate":
        generate([a.only] if a.only else ["off", "on"], a.topics or list(range(1, len(TOPICS) + 1)))
    elif a.cmd == "report":
        report()
    else:
        jev()


if __name__ == "__main__":
    main()
