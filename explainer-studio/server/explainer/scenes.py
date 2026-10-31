"""Lab mode (scene-plus): after the Interactive outline is written, design each section's interaction as a scene.

Per section, in parallel (labplan.py holds the questions and checks):
1. Jev decides whether an interaction helps (four narrow yes/no questions). Without a Jev key or on a Jev error,
   the old rule decides: sections holding a formula, control or live visual (CANDIDATE).
2. Haiku plans the lab (LAB_PLAN_SYSTEM, format labPlan): variables, buttons, visual parts, and "expects", which
   parts must change when each variable moves. Code checks the plan's shape.
3. Haiku builds the scene from the plan (SCENE_SYSTEM). The scene goes through the same repair and numeric checks
   as assembly (shared/scene.ts / core/scene.py), then check_build moves every variable and compares what each part
   draws, against the plan. Problems get one repair call with the exact messages.
Only a scene that survives replaces the blocks it lists: sliders, charts, plots, readouts, bars, experiments, never
text, callouts, formulas, quizzes or worked examples. A replaced control whose variable is still read by another
block (or by the scene) stays. Never raises: any failure leaves the section as it was, with a run note.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anthropic

from . import data
from .core.spec import parse_block
from .core.expr import try_compile
from .core.validate import declared_vars, probe_scopes, repair_block, sanity_check, slider_map
from .jev import JevError, jev_ask
from .json_util import extract_json
from .labplan import GATE_QUESTIONS, check_build, check_plan, gate_hint, gate_state, gate_verdict
from .llm import write_text
from .pricing import UsageLog

# Sections worth a lab when Jev can't be asked: they hold a formula, a control or a live visual.
CANDIDATE = {"formula", "slider", "choice", "toggle", "chart", "plot", "readout", "calculator", "samples", "heatmap", "experiment", "trajectory", "bars"}
# Blocks a scene may take over.
REPLACEABLE = {"slider", "choice", "toggle", "presets", "chart", "plot", "readout", "bars", "samples", "heatmap", "experiment", "calculator", "trajectory"}
CONTROLS = {"slider", "choice", "toggle"}
# Blocks that compute with a variable (a formula's symbol link or a paragraph naming it doesn't need its slider).
READERS = {"plot", "chart", "readout", "bars", "calculator", "table", "samples", "heatmap", "trajectory", "matrix", "experiment", "examples", "presets", "scene"}
# A lab added beside the section's parts goes before its closing questions.
QUESTIONS = {"quiz", "predict", "reveal"}
MAX_TOKENS = 6000
PLAN_TOKENS = 3000


def _controls(outline: dict) -> list[dict]:
    """The page's controls, as the designer sees them (any formula may read them by name)."""
    out = []
    for st in outline["stages"]:
        for b in st["blocks"]:
            if b.get("type") == "slider":
                out.append({"var": b["var"], "label": b["label"], "min": b["min"], "max": b["max"], "value": b["value"]})
            elif b.get("type") in ("choice", "toggle"):
                out.append({"var": b["var"], "label": b["label"], "value": b["value"]})
    return out


def _reads(blocks: list[dict], name: str) -> bool:
    """Whether any of these blocks computes with the variable (any string field of a block that reads variables)."""
    pat = re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])")
    return any(pat.search(json.dumps({k: v for k, v in b.items() if k != "var"}, ensure_ascii=False)) for b in blocks if b.get("type") in READERS)


# The fields that hold formulas (as in app/src/render/linked.ts): prose and labels never count as reading a variable.
FORMULA_KEYS = {"expr", "values", "from", "to", "init", "steps", "x", "y", "by", "trial", "truth", "markerX", "sweepVar",
                "visible", "disabled", "highlight", "if", "rows"}


def formula_vars(b: dict) -> set[str]:
    """The variables a block's formulas read (its own defs and a lab's own variables left out)."""
    out: set[str] = set()

    def walk(v: Any, key: str) -> None:
        if isinstance(v, str):
            if key not in FORMULA_KEYS or (key == "rows" and not v.startswith("=")):
                return
            src = v[1:] if v.startswith("=") else v
            r = try_compile(src) if src.strip() else {"ok": False}
            if r["ok"]:
                out.update(r["c"].vars)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)

    walk(b, "")
    own = {d.get("name") for d in b.get("defs") or [] if isinstance(d, dict)} | {s.get("name") for s in b.get("state") or [] if isinstance(s, dict)}
    return out - own


def orphaned(outline: dict, si: int, scene: dict, replaces: list[Any]) -> list[str]:
    """Sliders still shown in section si after the lab that a part there read before it and nothing there would read
    after it (worked examples and presets only set them): dragging them, or an example's Try it, would move nothing
    in their own section. A slider the lab replaces is gone, so it doesn't count."""
    blocks = outline["stages"][si]["blocks"]
    after = insert_scene(outline, si, scene, replaces)["stages"][si]["blocks"]
    skip = CONTROLS | {"examples", "presets"}

    def read_by(bs: list[dict]) -> set[str]:
        return set().union(*(formula_vars(b) for b in bs if b.get("type") not in skip)) if bs else set()

    before_reads, after_reads = read_by(blocks), read_by(after)
    return [f'the section\'s slider "{b.get("label", b["var"])}" would no longer move anything in this section: read {b["var"]} in the lab '
            f'(for example as where a Play counter stops, as a chart\'s x, or in a readout)'
            for b in after if b.get("type") == "slider" and b["var"] in before_reads and b["var"] not in after_reads]


def _section(outline: dict, si: int) -> dict:
    st = outline["stages"][si]
    return {
        "page": outline.get("title", ""), "level": outline.get("level", ""),
        "section": {"label": st.get("label", ""), "summary": st.get("blurb", ""), "blocks": [{"index": i, **b} for i, b in enumerate(st["blocks"])]},
        "pageControls": _controls(outline),
    }


def _page(outline: dict) -> tuple[list[dict], dict, dict[str, tuple[float, float]]]:
    """Every block, the page's values at their defaults, and its sliders' ranges."""
    every = [b for s in outline["stages"] for b in s["blocks"]]
    ranges = {b["var"]: (b["min"], b["max"]) for b in every if b.get("type") == "slider"}
    ranges.update({b["var"]: (0, 1) for b in every if b.get("type") == "toggle"})
    return every, probe_scopes(every)[0], ranges


# ---------- 1. does this section get a lab? ----------


def gate(jev_key: str | None, outline: dict, targets: list[int], usage: UsageLog, notes: list[str]) -> dict[int, dict]:
    """Per target stage: {"open", "why", "p"}. Falls back to the CANDIDATE rule without a key or on an error."""
    fallback = {si: {"open": any(b.get("type") in CANDIDATE for b in outline["stages"][si]["blocks"]), "why": None, "p": {}} for si in targets}
    if not jev_key or not targets:
        if targets:
            notes.append("Lab: no Jev key, so sections with a formula, control or chart were given labs.")
        return fallback

    def ask(si: int) -> dict | None:
        try:
            return jev_ask(jev_key, gate_state(outline, si), GATE_QUESTIONS)
        except JevError:
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        results = list(pool.map(ask, targets))
    out: dict[int, dict] = {}
    total, model = 0, "jev"
    for si, r in zip(targets, results):
        if r is None or not r["answers"]:
            out[si] = fallback[si]
            notes.append(f'Lab ({outline["stages"][si].get("label", "")}): Jev didn\'t answer, so the formula/control rule decided.')
            continue
        total += r["usage"].get("input_tokens", 0)
        model = r["model"]
        out[si] = gate_verdict(r["answers"])
    if total:
        usage.add("jev lab gate", model, {"input_tokens": total, "output_tokens": 0})
    return out


# ---------- 2. the plan ----------


def plan_lab(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, why: str | None) -> Any:
    user = json.dumps({**_section(outline, si), "hint": gate_hint(why)}, ensure_ascii=False)
    try:
        text = write_text(client, usage, "lab plan", system=data.prompt("LAB_PLAN_SYSTEM"), user=user, max_tokens=PLAN_TOKENS,
                          fmt=data.formats()["labPlan"])
        return json.loads(text)
    except (ValueError, RuntimeError, anthropic.APIError):
        return None


# ---------- 3. the build ----------


def _build(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, plan: dict,
           previous: dict | None = None, problems: list[str] | None = None) -> dict | None:
    payload: dict = {**_section(outline, si), "plan": plan}
    if previous is not None:
        payload["yourPreviousScene"] = previous
        payload["problems"] = problems or []
        payload["task"] = "Your previous scene broke these promises of the plan. Reply with the corrected scene (same format), keeping everything that worked."
    try:
        text = write_text(client, usage, "scene" if previous is None else "scene repair", system=data.prompt("SCENE_SYSTEM"),
                          user=json.dumps(payload, ensure_ascii=False), max_tokens=MAX_TOKENS)
        reply, _ = extract_json(text)
    except (ValueError, RuntimeError, anthropic.APIError):
        return None
    return reply if isinstance(reply, dict) else None


def checked_scene(outline: dict, raw: Any) -> tuple[dict | None, list[str], str]:
    """(scene after the assembly checks or None, its warnings, why it failed)."""
    if not isinstance(raw, dict):
        return None, [], "none"
    ok, scene = parse_block({**raw, "type": "scene"})
    if not ok:
        return None, [], "format"
    every = [b for s in outline["stages"] for b in s["blocks"]]
    warnings: list[str] = []
    checked = repair_block(scene, declared_vars(outline["stages"]), warnings.append)
    if checked:
        checked = sanity_check(checked, probe_scopes(every), warnings.append, slider_map(every))
    return checked, warnings, "" if checked else "checks"


def insert_scene(outline: dict, si: int, scene: dict, replaces: list[Any]) -> dict:
    """The outline with the scene in stage si, in place of the replaceable blocks it takes over."""
    st = outline["stages"][si]
    blocks = st["blocks"]
    asked = [i for i in replaces or [] if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(blocks) and blocks[i].get("type") in REPLACEABLE]
    replaced = set(asked)
    # A control stays when another block on the page, or the scene itself, still reads its variable.
    others = [b for s2i, s2 in enumerate(outline["stages"]) for bi, b in enumerate(s2["blocks"]) if not (s2i == si and bi in replaced)]
    for i in sorted(replaced):
        b = blocks[i]
        if b.get("type") in CONTROLS and (_reads(others, b["var"]) or _reads([scene], b["var"])):
            replaced.discard(i)
    if replaced:
        at = min(replaced)
    else:  # added beside the section's parts: before its closing questions
        at = len(blocks)
        while at > 0 and blocks[at - 1].get("type") in QUESTIONS:
            at -= 1
    new = [b for i, b in enumerate(blocks) if i not in replaced]
    new.insert(sum(1 for i in range(at) if i not in replaced), scene)
    stages = list(outline["stages"])
    stages[si] = {**st, "blocks": new}
    return {**outline, "stages": stages}


def place_scene(outline: dict, si: int, reply: dict, notes: list[str]) -> dict:
    """The outline with the reply's scene placed in stage si (if it passes the assembly checks), else unchanged."""
    label = outline["stages"][si].get("label", "")
    scene, warnings, failed = checked_scene(outline, reply.get("scene"))
    if failed == "format":
        notes.append(f'Lab ({label}): the designed lab didn\'t match the format, so the section keeps its parts.')
    elif failed == "checks":
        notes.append(f"Lab ({label}): the designed lab failed its checks, so the section keeps its parts." + (f" ({warnings[0]})" if warnings else ""))
    if not scene:
        return outline
    notes.extend(f"Lab ({label}): {w}" for w in warnings)
    return insert_scene(outline, si, scene, reply.get("replaces") or [])


def _design_one(client: anthropic.Anthropic, usage: UsageLog, outline: dict, si: int, why: str | None) -> dict:
    """Plan, build, check, repair once. {"reply"?: the scene reply to place, "notes", "planned", "repaired"}."""
    label = outline["stages"][si].get("label", "")
    res: dict = {"notes": [], "planned": False, "repaired": False}
    raw_plan = plan_lab(client, usage, outline, si, why)
    if raw_plan is None:
        res["notes"].append(f"Lab ({label}): the lab planner didn't answer, so the section keeps its parts.")
        return res
    plan, problems = check_plan(raw_plan, declared_vars(outline["stages"]), len(outline["stages"][si]["blocks"]))
    if plan is None and not problems:
        return res  # the planner found nothing to vary
    if problems:
        res["notes"].append(f"Lab ({label}): the lab plan wasn't usable, so the section keeps its parts. ({problems[0]})")
        return res
    res["planned"] = True
    every, page_scope, ranges = _page(outline)
    labels = {b["var"]: b.get("label", "") for b in every if b.get("type") == "slider"}
    reply = _build(client, usage, outline, si, plan)
    for attempt in (0, 1):
        if reply is None:
            res["notes"].append(f"Lab ({label}): the lab builder didn't answer, so the section keeps its parts.")
            return res
        scene, warnings, failed = checked_scene(outline, reply.get("scene"))
        broken = check_build(scene, plan, page_scope, ranges, labels) if scene else [f"the scene failed its checks ({warnings[0] if warnings else failed})"]
        if scene:
            broken += orphaned(outline, si, scene, reply.get("replaces") or plan["replaces"])
        # A part or formula the checks dropped is a promise the scene no longer keeps.
        if scene:
            broken += [w for w in warnings if "dropped" in w.lower() and w not in broken]
        if not broken:
            res["reply"] = {"scene": reply["scene"], "replaces": reply.get("replaces") or plan["replaces"]}
            return res
        if attempt == 0:
            res["repaired"] = True
            # The checks' own warnings say why a part was dropped (a formula that never gives a number...).
            reply = _build(client, usage, outline, si, plan, reply.get("scene"), [*broken, *(w for w in warnings if w not in broken)])
        else:
            res["notes"].append(f"Lab ({label}): the built lab didn't do what its plan promised, so the section keeps its parts. ({broken[0]})")
    return res


def design_scenes(client: anthropic.Anthropic, outline: dict, usage: UsageLog, notes: list[str], only: list[int] | None = None,
                  jev_key: str | None = None) -> tuple[dict, dict]:
    """The outline with a scene in each section where one was planned, built and passed. Returns (outline, stats):
    stats = {"sections", "opened" (by the gate), "planned", "repaired", "placed"}.
    only: stage indices to consider (a revision designs labs for its new or replaced stages only)."""
    targets = [i for i, st in enumerate(outline["stages"])
               if (only is None or i in only) and not any(b.get("type") == "scene" for b in st["blocks"])]
    stats = {"sections": len(targets), "opened": 0, "planned": 0, "repaired": 0, "placed": 0}
    verdicts = gate(jev_key, outline, targets, usage, notes)
    opened = [si for si in targets if verdicts[si]["open"]]
    stats["opened"] = len(opened)
    if not opened:
        return outline, stats
    with ThreadPoolExecutor(max_workers=min(8, len(opened))) as pool:
        results = list(pool.map(lambda si: _design_one(client, usage, outline, si, verdicts[si]["why"]), opened))
    for si, r in zip(opened, results):
        notes.extend(r["notes"])
        stats["planned"] += r["planned"]
        stats["repaired"] += r["repaired"]
        if "reply" in r:
            before = outline
            outline = place_scene(outline, si, r["reply"], notes)
            stats["placed"] += outline is not before
    return outline, stats
