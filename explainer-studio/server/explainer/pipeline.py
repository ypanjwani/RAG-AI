"""The generation pipeline: a fixed sequence of Claude calls, not an agent.

  paper      -> cut references; Jev may skip side-material sections (filter.py);
                Haiku summarizes section groups (in parallel)
  all        -> Haiku writes the outline (JSON, or the compact format for Lite+);
                it may call the "compute" tool to check numbers against our math engine
  design     -> Haiku picks it from enum menus (Jev's page brief may override)
  freeform+  -> the outline has briefs for interactive parts; Haiku writes each
                widget's HTML/JS (they run sandboxed in the browser)
  code       -> validate/repair and assemble the spec (core/validate.py)
  all        -> Haiku writes worked-example steps from numbers code computed
  quality 2.0 -> clarity check (clarity.py): Jev judges each stage, Haiku adds or simplifies
  quality+, 2.0 -> fact check (factcheck.py)
The Effort toggle (low | medium | high) sets planning effort; widget writing goes no
higher than medium. Every call's token usage is logged and priced (pricing.py).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import anthropic

from . import data
from .addsection import addition_request, parse_addition, place_detail, place_section
from .chartpick import chart_on, experiments_to_charts
from .codelab_edit import page_kind
from .codelab import PLANNER_NOTE, PLANNER_NOTE_4, PLANNER_NOTE_PG, CodeLabSettings, Streamed, design_code_widgets, strip_prebuilt
from .scenes import design_scenes
from .brief import ask_brief, brief_hint, brief_summary
from .core.compact import parse_compact
from .core.examples import apply_example_steps, example_briefs
from .core.expr import eval_value, is_mat, is_num
from .core.followup import CHANGE_RULES, continuation_note, revision_request
from .core.lab import briefs_of, merge_bundles, parse_lab_outline
from .core.references import cut_references
from .core.schema import parse as schema_parse
from .core.spec import parse_design, parse_outline
from .core.validate import MAX_STAGES, assemble_spec, default_design, requested_stages, without_blocks
from .design import apply_brief, apply_prefs, design_with_brief
from .clarity import clarity_check
from .factcheck import fact_check
from .feedback import prefs_hint
from .notes import note_fields, write_notes
from .filter import filter_sections
from .paper_cache import paper_key
from .revise import improve_request, parse_revision, restore_kept_widgets, revision_summary
from . import feedback_check
from .targets import apply_targets, clean_targets, find_targets
from . import smart as smart_mode
from .js import WS, is_finite, js_len, js_round, js_slice, js_str, js_trim, json_parse, json_stringify, round_num
from .json_util import extract_json
from .llm import HAIKU, ReaderError, cached_system, check_stop, text_of, write_text
from .pricing import UsageLog

log = logging.getLogger(__name__)

GROUP_CHARS = 14_000  # section text per Haiku summary call
CONTEXT_CHARS = 24_000  # context kept for the chatbot
MAX_TOOL_ROUNDS = 4  # compute-tool round trips before the final answer is required
# Evals: FACTCHECK_SECTIONS=1 adds the "consistent with its own section" question to the fact check.
# Off until hand-labelled pages show it catches mismatches the other two questions miss.
FACTCHECK_SECTIONS = os.environ.get("FACTCHECK_SECTIONS") == "1"

Emit = Callable[[dict], None]


def _step(emit: Emit, step: str, status: str, detail: str | None = None) -> None:
    e = {"type": "step", "step": step, "status": status}
    if detail:
        e["detail"] = detail
    emit(e)


def _prompt(name: str) -> str:
    return data.prompt(name)


def planner_system(force_chart: bool = False) -> str:
    """The Quality+ planner's prompt: with the chart block (OUTLINE_CHART_SYSTEM) unless CHART_BLOCK=0 (chartpick.chart_on);
    Lab mode always uses it (force_chart). Quality 2.0's audit uses the same prompt so its cache is read by the improve plan."""
    return _prompt("OUTLINE_CHART_SYSTEM" if force_chart or chart_on() else "OUTLINE_SYSTEM")


# ---------- paper summaries ----------


def group_sections(sections: list[dict]) -> list[str]:
    """Merge sections into groups of about GROUP_CHARS so long papers make few calls."""
    groups: list[str] = []
    cur = ""
    for s in sections:
        piece = f"## {s['heading']}\n{s['text']}\n\n"
        if cur and js_len(cur) + js_len(piece) > GROUP_CHARS:
            groups.append(cur)
            cur = ""
        cur += js_slice(piece, 0, int(GROUP_CHARS * 1.5)) if js_len(piece) > GROUP_CHARS * 1.5 else piece
    if cur:
        groups.append(cur)
    return groups


def summarize(client: anthropic.Anthropic, group: str, usage: UsageLog) -> str:
    return js_trim(write_text(client, usage, "summarize", system=_prompt("SUMMARIZE_SYSTEM"), user=group, max_tokens=2000, allow_cutoff=True))


# ---------- compute tool: the planner checks numbers with our math engine ----------

_COMPUTE_INPUT = {"t": "object", "shape": {"expressions": {"t": "array", "element": {"t": "object", "shape": {
    "expr": {"t": "string"},
    "vars": {"t": "optional", "inner": {"t": "array", "element": {"t": "object", "shape": {"name": {"t": "string"}, "value": {"t": "number", "int": False}}}}},
}}}}}


def show_value(v: Any) -> str:
    def n(x: Any) -> str:
        return js_str(round_num(x, 6)) if is_finite(x) else "NaN (not a valid number)"

    if is_num(v) or v is None:
        return n(v)
    if is_mat(v):
        return "[" + ", ".join("[" + ", ".join(n(x) for x in r[:8]) + "]" for r in v[:8]) + "]"
    return "[" + ", ".join(n(x) for x in v[:12]) + (f", … ({len(v)} items)" if len(v) > 12 else "") + "]"


def run_compute(tool_input: Any) -> dict:
    """One compute call; the input is validated here before any expression runs. {"content", "is_error"}."""
    value, issues = schema_parse(_COMPUTE_INPUT, tool_input)
    if not issues and len(value["expressions"]) > 40:
        issues = [{"message": "Too big: expected array to have <=40 items"}]
    if issues:
        return {"content": f"Invalid input: {issues[0]['message'] if issues else 'bad shape'}", "is_error": True}
    lines = []
    for e in value["expressions"]:
        scope: dict = {"seed": 0.0}
        for var in e.get("vars") or []:
            scope[var["name"]] = var["value"]
        lines.append(f"{e['expr']} = {show_value(eval_value(e['expr'], scope))}")
    return {"content": "\n".join(lines), "is_error": False}


def read_reply(text: str, fmt: str) -> tuple[Any, bool]:
    """The planner's reply as a raw object: JSON, or the compact format (a reply that starts with "{" is read as JSON either way)."""
    if fmt == "json" or re.match(WS + r"*(```(json)?" + WS + r"*)?\{", text):
        return extract_json(text)
    return parse_compact(text), False


# Compute-tool round trips allowed per plan attempt. Each round resends the whole conversation.
FULL_BUDGET = {"rounds": MAX_TOOL_ROUNDS, "retryRounds": MAX_TOOL_ROUNDS}
# Lite+: at most 2 rounds; a retry gets none and is handed the numbers the first attempt checked.
LITE_BUDGET = {"rounds": 2, "retryRounds": 0}


def write_outline(
    client: anthropic.Anthropic, request: str | list[dict], emit: Emit, usage: UsageLog, system: str,
    parse: Callable[[Any], dict], effort: str, fmt: str = "json", budget: dict = FULL_BUDGET,
    whole: str = "the whole outline", on_text: Callable[[str | None], None] | None = None,
) -> dict:
    """{"outline", "warnings", "revision"?} from the planner. One attempt normally; a second (full rewrite) only when
    the reply is unusable even after JSON repair and outline defaults. whole: what the reply holds, for the nudge
    after the last compute round ("your reply" for a revision made as changes). "revision" is passed through
    from parse (revise.parse_revision). on_text, if given, sees the reply as it streams: None when a new stream starts
    (a compute round or a retry), then each text delta (Code Lab starts a section's widget as soon as it is written)."""
    last_error = ""
    checked: list[str] = []  # every compute result, so a retry without the tool can still quote checked numbers
    compute_tool = data.formats()["computeTool"]
    for attempt in range(2):
        label = "plan (retry)" if attempt else "plan"
        max_rounds = budget["retryRounds"] if attempt else budget["rounds"]
        carried = ("\n\nThe compute tool is off for this reply. Numbers you already checked:\n" + js_slice("\n".join(checked), 0, 4000)) if attempt and max_rounds == 0 and checked else ""
        if last_error:
            again = "Reply again with the whole outline in the compact format." if fmt == "compact" else "Reply again with one valid JSON object."
            note = f"Your previous reply could not be used ({last_error}). {again}{carried}"
            # A block request (Quality 2.0's improve) keeps its cached page block first.
            content = [*request, {"type": "text", "text": note}] if isinstance(request, list) else f"{request}\n\n{note}"
        else:
            content = request
        messages: list[dict] = [{"role": "user", "content": content}]
        chars = last_report = checks = 0
        final = None

        for rnd in range(max_rounds + 1):
            try:
                with client.beta.messages.stream(
                    model=HAIKU,
                    max_tokens=48_000,
                    output_config={"effort": effort},
                    system=cached_system(system),
                    # Automatic caching of the conversation so far (the request, which for a revision or an
                    # addition carries the page, plus earlier compute rounds): each compute round reads it at a
                    # tenth of the price instead of paying for it again. The system prompt keeps its own marker.
                    cache_control={"type": "ephemeral"},
                    tools=[compute_tool],
                    # After the last allowed round, the model must answer.
                    tool_choice={"type": "none"} if rnd == max_rounds else {"type": "auto"},
                    messages=messages,
                ) as stream:
                    if on_text:
                        on_text(None)
                    # Report progress as the reply streams in (also keeps the SSE connection busy).
                    for t in stream.text_stream:
                        if on_text:
                            on_text(t)
                        chars += len(t)
                        if chars - last_report > 1500:
                            last_report = chars
                            _step(emit, "plan", "running", f"Writing lesson… {js_str(js_round(chars / 1000))}k characters")
                    msg = stream.get_final_message()
            except anthropic.APIError:
                # API errors (rate limit, auth, overload…) still fail the run.
                raise
            except Exception:
                # Anything else is the SDK failing to read a malformed tool call: retry instead.
                log.exception("plan stream failed")
                last_error = "a malformed compute tool call"
                break
            usage.add_message(f"{label} (after compute)" if rnd else label, msg)
            check_stop(msg, "lesson outline")
            if msg.stop_reason != "tool_use":
                final = msg
                break

            calls = [b for b in msg.content if b.type == "tool_use"]
            # Seen once (Oct 2026): stop_reason "tool_use" with no tool call. Answering would send an
            # empty user turn, which the API rejects (400), so retry the plan instead.
            if not calls:
                last_error = "a tool call was announced but missing"
                break
            # Append the full response (thinking blocks included) and answer every tool call.
            messages.append({"role": "assistant", "content": [b.to_dict(exclude_none=True) for b in msg.content]})
            results = []
            for c in calls:
                r = run_compute(c.input) if c.name == "compute" else {"content": "Unknown tool.", "is_error": True}
                if not r["is_error"]:
                    checked.append(r["content"])
                results.append({"type": "tool_result", "tool_use_id": c.id, **r})
            # The next round has the tool switched off. Unless told, Haiku tries to keep checking and
            # ends with an empty reply (stop=end_turn, 0 chars), which cost a full retry.
            if rnd + 1 == max_rounds:
                results.append({"type": "text", "text": f"That was the last compute check. Write {whole} now" + (" in the compact format." if fmt == "compact" else " as one JSON object.")})
            messages.append({"role": "user", "content": results})
            checks += len(calls)
            _step(emit, "plan", "running", f"Checking numbers… ({checks})")
        if final is None:
            last_error = last_error or "no final answer after checking numbers"
            continue

        reply = text_of(final)
        try:
            value, repaired = read_reply(reply, fmt)
            parsed = parse(value)
            if "outline" in parsed and parsed["outline"]["stages"]:
                notes = []
                if attempt:
                    notes.append(f"Plan retried once: the first reply could not be used ({last_error}).")
                if repaired:
                    notes.append("The plan's JSON had a syntax slip and was repaired automatically.")
                return {"outline": parsed["outline"], "warnings": [*notes, *parsed["warnings"]],
                        **({"revision": parsed["revision"]} if "revision" in parsed else {})}
            last_error = js_slice(parsed["error"], 0, 600) if "error" in parsed else "no stages"
        except Exception as e:
            last_error = js_slice(str(e), 0, 300) or "invalid JSON"
        # Why a plan was unusable (a cut-off reply, prose instead of JSON, a broken object…).
        log.warning(
            "plan attempt %d unusable: %s | stop=%s | after %d compute calls | content [%s] | out %d tokens | %d chars | starts: %s | ends: %s",
            attempt + 1, last_error, final.stop_reason, checks, ", ".join(b.type for b in final.content), final.usage.output_tokens,
            len(reply), reply[:200], reply[-200:],
        )
    raise ReaderError("Couldn't produce a valid lesson outline. Please try again.")


# ---------- design and worked examples ----------


def design_brief(outline: dict, request: str) -> str:
    stages = "\n".join(f"- {s['id']}: {s['label']} — {s['blurb']}" for s in outline["stages"])
    return "\n".join([
        f"Title: {outline['title']}",
        f"Subtitle: {outline['subtitle']}",
        f"Level: {outline['level']}",
        f"Key intuition: {outline['keyIntuition']}",
        f"Stages:\n{stages}",
        f"Original request (excerpt): {js_slice(request, 0, 1500)}",
    ])


def choose_design(client: anthropic.Anthropic, outline: dict, request: str, usage: UsageLog) -> dict | None:
    try:
        msg = client.messages.create(
            model=HAIKU,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": data.formats()["design"]}},
            system=cached_system(_prompt("DESIGN_SYSTEM")),
            messages=[{"role": "user", "content": design_brief(outline, request)}],
        )
        usage.add_message("design", msg)
        check_stop(msg, "design")
        ok, design = parse_design(json_parse(text_of(msg)))
        if not ok:
            raise ValueError(design[0]["message"])
        return design
    except anthropic.APIError:
        log.exception("design step failed")
        return None  # assemble_spec falls back to the default design
    except Exception:
        log.exception("design step failed")
        return None


_STEPS_OUT = {"t": "object", "shape": {"examples": {"t": "array", "element": {"t": "object", "shape": {
    "key": {"t": "string"}, "steps": {"t": "array", "element": {"t": "string"}}}}}}}
_BRIEF_TEXT_OUT = {"t": "object", "shape": {"items": {"t": "array", "element": {"t": "object", "shape": {
    "key": {"t": "string"}, "md": {"t": "string"}}}}}}


def _parse_format(node: dict, text: str) -> Any:
    value, issues = schema_parse(node, json_parse(text))
    if issues:
        raise ValueError(issues[0]["message"])
    return value


def write_examples(client: anthropic.Anthropic, spec: dict, usage: UsageLog) -> dict:
    """Haiku writes worked-example steps around numbers our code computed. Failures keep the "focus" line only."""
    briefs = example_briefs(spec)
    if not briefs:
        return spec
    try:
        text = write_text(client, usage, "examples", system=_prompt("EXAMPLES_SYSTEM"),
                          user=f"Explainer: {spec['title']} (level: {spec['level']})\n\nExamples:\n{json_stringify(briefs, indent=1)}",
                          max_tokens=6000, fmt=data.formats()["steps"])
        out = _parse_format(_STEPS_OUT, text)
        return apply_example_steps(spec, {e["key"]: e["steps"] for e in out["examples"]})
    except Exception:
        log.exception("examples step failed")
        return spec


# ---------- Freeform+: Haiku writes each widget's code ----------

_FENCE = re.compile(r"```(?:html)?[^\n]*\n([\s\S]*?)(?:```|\Z)", re.I)
_TITLE = re.compile(r"^<!--" + WS + r"*title:" + WS + r"*([\s\S]*?)-->", re.I)


def parse_widget(text: str, fallback_title: str) -> dict:
    """The widget from a reply: the ```html block (or the whole reply) and its "<!-- title: … -->" line."""
    m = _FENCE.search(text)
    html = js_trim(m.group(1) if m else text)
    t = _TITLE.match(html)
    if t:
        html = js_trim(html[t.end():])
    title = js_slice(js_trim(t.group(1)), 0, 80) if t else ""
    return {"title": title or fallback_title, "html": html}


def briefs_to_text(client: anthropic.Anthropic, refs: list[dict], lab: dict, usage: UsageLog) -> dict[str, list[dict]]:
    """Briefs whose widget failed become a short paragraph each (one batched Haiku call)."""
    out: dict[str, list[dict]] = {}
    if not refs:
        return out
    try:
        user = json_stringify({"explainer": lab["title"], "level": lab["level"],
                               "briefs": [{"key": r["key"], "stage": r["stageLabel"], **r["brief"]} for r in refs]})
        text = write_text(client, usage, "brief to text", system=_prompt("BRIEF_TO_TEXT"), user=user, max_tokens=4000, fmt=data.formats()["briefText"])
        for it in _parse_format(_BRIEF_TEXT_OUT, text)["items"]:
            md = js_trim(it["md"])
            if md:
                out[it["key"]] = [{"type": "text", "md": md}]
    except Exception:
        log.exception("brief-to-text failed")
    for r in refs:
        if r["key"] not in out:
            out[r["key"]] = [{"type": "text", "md": f"**{r['brief']['goal']}** {r['brief']['insight']}"}]
    return out


def write_widget(client: anthropic.Anthropic, ref: dict, lab: dict, usage: UsageLog, effort: str) -> dict | None:
    """Haiku writes one self-contained widget for a brief. None if it can't."""
    try:
        msg = client.messages.create(
            model=HAIKU,
            max_tokens=16_000,
            output_config={"effort": effort},
            system=cached_system(_prompt("WIDGET_SYSTEM")),
            messages=[{"role": "user", "content": json_stringify({"explainer": lab["title"], "level": lab["level"], "stage": ref["stageLabel"], "brief": ref["brief"]})}],
        )
        usage.add_message("widget", msg)
        check_stop(msg, "widget")
        goal = ref["brief"]["goal"]
        fallback = f"{js_slice(goal, 0, 57)}…" if js_len(goal) > 60 else goal
        return {"type": "widget", **parse_widget(text_of(msg), fallback)}
    except Exception:
        log.exception("widget failed")
        return None


def bundle_works(bundle: list[dict], btype: str, base: dict) -> bool:
    """Does the bundle survive validation on its own (structure and number checks)?"""
    phase = base["phases"][0]["id"] if base["phases"] else "main"
    mini = {**base, "stages": [{"id": "probe", "label": "probe", "phase": phase, "blurb": "", "blocks": bundle}]}
    spec = assemble_spec(mini, None, "")
    return bool(spec["stages"]) and any(b["type"] == btype for b in spec["stages"][0]["blocks"])


def build_widgets(client: anthropic.Anthropic, lab: dict, usage: UsageLog, emit: Emit, warnings: list[str], effort: str) -> dict[str, list[dict]]:
    """Every brief becomes an AI-written widget; ones that fail become a paragraph."""
    refs = briefs_of(lab)
    bundles: dict[str, list[dict]] = {}
    failed: list[dict] = []
    lock = threading.Lock()
    done = [0]
    _step(emit, "components", "running", f"0 / {len(refs)} widgets written")

    def one(ref: dict) -> None:
        w = write_widget(client, ref, lab, usage, effort)
        ok = w is not None and bundle_works([w], "widget", lab)
        with lock:
            if ok:
                bundles[ref["key"]] = [w]
            else:
                failed.append(ref)
            done[0] += 1
            n = done[0]
        _step(emit, "components", "running", f"{n} / {len(refs)} widgets written")

    if refs:
        with ThreadPoolExecutor(max_workers=min(16, len(refs))) as pool:
            list(pool.map(one, refs))
    if failed:
        failed.sort(key=lambda r: refs.index(r))
        warnings.append(f"{len(failed)} widget(s) couldn't be written and became a short paragraph.")
        bundles.update(briefs_to_text(client, failed, lab, usage))
    return bundles


# ---------- the run ----------


def outline_context(o: dict) -> str:
    lines = [f"Explainer: {o['title']} — {o['subtitle']}", f"Key intuition: {o['keyIntuition']}"]
    for s in o["stages"]:
        lines.append(f"\n## {s['label']}\n{s['blurb']}")
        for b in s["blocks"]:
            if b["type"] in ("text", "callout"):
                lines.append(b["md"])
            elif b["type"] == "formula":
                lines.append(f"Formula: {b['latex']} ({b['caption']})")
            elif b["type"] == "cards":
                lines.extend(f"{it['title']}: {it['md']}" for it in b["items"])
    lines.append("\nGlossary:\n" + "\n".join(f"- {g['term']}: {g['definition']}" for g in o["glossary"]))
    return "\n".join(lines)


def interactivity_hint(i: str | None) -> str:
    """The composer's Mostly reading | Balanced | Hands-on toggle, as a line in the planner's request (the user turn,
    so the cached system prompts stay byte-stable). Balanced adds nothing."""
    if i == "reading":
        return "The reader chose a mostly-reading page: clear, well-structured explanation with only a few visuals or interactive parts, placed where they help most. Keep the quizzes."
    if i == "hands-on":
        return "The reader chose a hands-on page: give most stages an interactive part (sliders with live charts, experiments, predict-then-reveal and the like) wherever the topic allows, so they learn by trying."
    return ""


def drops_examples(extras: list[str] | None) -> bool:
    """The reader answered "What do you want to add to each section?" without worked examples (None included).
    Absent (not asked: older apps, scripts) keeps the planner's usual examples, and so does auto."""
    return extras is not None and "examples" not in extras and "auto" not in extras


def extras_hint(extras: list[str] | None) -> str:
    """The worked-examples part of the answer, as a line in the planner's request. Deeper explanation and other
    wording are written after the plan (notes.py) and web search runs on click, so neither changes the plan."""
    if extras is None or "auto" in extras:
        return ""
    if "examples" in extras:
        return ("The reader asked for worked examples: give every stage that has numbers, a formula or a procedure an "
                "examples block with one or two worked examples.")
    return "The reader chose no worked examples: write no examples blocks."


def section_tools(extras: list[str] | None) -> list[str]:
    """The buttons that call the server when clicked (POST /api/section). auto offers web search: it costs nothing
    until clicked."""
    e = extras or []
    return ["search"] if "search" in e or "auto" in e else []


def generate(client: anthropic.Anthropic, req: dict, emit: Emit, jev_key: str | None = None, plan_effort: str = "medium",
             paper_cache: Any = None, usage: UsageLog | None = None, checks: bool = True) -> dict:
    """{"spec", "usage"} for one GenerateRequest. Progress goes to emit().

    paper_cache: optional get(key)/put(key, value) store (paper_cache.PaperStore) for a paper's filter and summaries.
    usage: log into this one instead of a new log (Quality 2.0 runs several generations as one, deepen.py).
    checks: False skips the clarity and fact check (Quality 2.0 verifies and audits the page itself)."""
    quality = req.get("quality") or "quality-plus"
    # Lab mode (scene-plus, scenes.py): an Interactive page, then each section's interaction designed as a scene.
    scenes = quality == "scene-plus"
    if scenes:
        quality = "quality-plus"
    # Code Lab (code-lab, codelab.py): Haiku plans and writes the prose only; as each section is written, Jev gates it,
    # Haiku (High) plans and builds its widget, and Jev reviews it (repair, backup or text). Code Lab 2.0 (code-lab-2):
    # the same, with the page's one Effort for every Haiku step and Vega-Lite charts for chart-like sections. Code Lab 3.0
    # (code-lab-3): Code Lab 2.0 with the chart-only plan, its charts written as full Vega (CodeLabSettings.full_vega).
    # Code Lab 7.0 (code-lab-7): the writer marks which sections get an interactive part (PLANNER_NOTE_4), Haiku's own code
    # (plan, build, repairs, backup) designed freely like Creative's, and Jev checking the page for repeats.
    # Playground (playground): 7.0's marks and Jev checks, each part a playground spec (codelab._design_playground).
    lab_mode = quality if quality in ("code-lab", "code-lab-2", "code-lab-3", "code-lab-7", "playground") else None
    code_lab = lab_mode is not None and not (req.get("followUp") and req["followUp"]["kind"] == "revise")  # a revision is a plain Quality+ revision
    if lab_mode:
        quality = "quality-plus"
    follow = req.get("followUp")
    # Smart mode (smart.py) plans new pages only; a revision of one is a Quality+ revision made as changes.
    if quality == "smart" and follow and follow["kind"] == "revise":
        quality = "quality-plus"
    smart = quality == "smart"
    # Lite+: a static, mostly-reading page planned with its own short prompt in the compact format, always on Low.
    lite = quality == "lite-plus"
    effort = "low" if lite else (req.get("effort") or plan_effort or "medium")
    fill_effort = "low" if effort == "low" else "medium"
    label = "scene-plus" if scenes else lab_mode if code_lab else quality
    if usage is None:
        usage = UsageLog(label, effort)
    else:
        usage.label(label, effort)
    lab_settings = (CodeLabSettings.for_effort(effort, think=req.get("planThinking", True), slim=lab_mode == "code-lab-3",
                                               full_vega=lab_mode == "code-lab-3",
                                               free=lab_mode == "code-lab-7", playground=lab_mode == "playground")
                    if lab_mode in ("code-lab-2", "code-lab-3", "code-lab-7", "playground") else CodeLabSettings())
    freeform = quality == "freeform-plus"
    early_warnings: list[str] = []
    rejected: list[dict] = []  # Code Lab: widgets Jev's review rejected (returned for test runs only, debugRejected)
    lab_marks: dict = {}  # Code Lab 4.0: which sections the writer marked for a chart (test runs only, debugRejected)
    lab_math: list = []  # Code Lab 4.0: each chart's maths review (test runs only, debugRejected)
    lab_look: list = []  # Code Lab 7.0: each widget's look check (test runs only, debugRejected)
    # A Quality+ revision replies with only its changes, merged into the previous outline (revise.py); Lite+ and
    # Freeform+ revisions (their own planners and formats) still write the whole outline.
    base_outline = None  # the previous version's outline (placeholders for its widgets), whatever the mode
    if follow and follow["kind"] == "revise":
        try:
            o = json_parse(follow["outline"])
            base_outline = o if isinstance(o, dict) and isinstance(o.get("stages"), list) and o["stages"] else None
        except ValueError:
            base_outline = None
    old_outline = base_outline if quality == "quality-plus" else None
    # "Add section" (addsection.py): Jev picks where the new stage goes, before Haiku writes it.
    adding = old_outline is not None and follow.get("add") is True
    # A stage count the reader typed ("14 stages") beats the prompt's 4-8 default (not for an addition).
    asked = None if adding else requested_stages(req["note"] if req["mode"] == "paper" else req["text"])
    if asked and asked["clamped"]:
        early_warnings.append(f"Explainers have at most {MAX_STAGES} stages, so this one was planned with {MAX_STAGES}.")
    hint = "\n\n".join(x for x in [
        "" if lite else interactivity_hint(req.get("interactivity")),
        extras_hint(req.get("extras")),
        f"The reader asked for {asked['count']} stages: write exactly {asked['count']}." if asked else "",
        # The reader's saved preferences (feedback.py); "" for most readers. Not for a feedback change, which does only
        # what the reader confirmed.
        "" if follow and follow.get("feedback") else prefs_hint(req.get("prefs")),
    ] if x)

    with ThreadPoolExecutor(max_workers=8) as pool:
        # Quality+/Lite+: Jev's page brief, asked now so it runs alongside the PDF filter and summaries.
        brief_future = None
        if quality in ("quality-plus", "lite-plus", "smart"):
            def get_brief() -> dict:
                b = ask_brief(jev_key, req, usage, early_warnings)
                if lite:  # Lite+ drops the random-process check, whose lines are about experiment blocks.
                    b = {**b, "shape": {k: v for k, v in b["shape"].items() if k != "random"}}
                return b
            brief_future = pool.submit(get_brief)

        def brief_line() -> str:
            b = brief_future.result() if brief_future else None
            return brief_hint(b) if b else ""

        source_context = ""
        if req["mode"] == "paper":
            # Every mode: drop the reference list (and what follows it) before anything is summarized.
            refs = cut_references(req["sections"])
            sections = refs["sections"]
            ref_note = f" · references cut ({js_str(js_round(refs['removed'] / 1000))}k chars)" if refs["removed"] else ""
            # The same paper read before: its filter result and summaries come from the cache (paper_cache.py).
            key = paper_key(req["title"], sections) if paper_cache is not None else ""
            hit = paper_cache.get(key) if paper_cache is not None else None
            if hit:
                if hit["skipped"]:
                    skipped = ", ".join(f'"{js_slice(h, 0, 50)}"' for h in hit["skipped"])
                    early_warnings.append(f"Jev skipped {len(hit['skipped'])} of {len(sections)} sections as side material: {skipped}.")
                _step(emit, "read", "done", f"kept {hit['kept']} of {len(sections)} sections (read before){ref_note}")
                _step(emit, "summarize", "done", f"{hit['groups']} parts (from cache)")
                useful = hit["summaries"]
            else:
                _step(emit, "read", "running", f"Jev is checking {len(sections)} sections{ref_note}")
                f = filter_sections(jev_key, req["title"], sections, usage)
                kept = f["sections"]
                if f["skipped"]:
                    skipped = ", ".join(f'"{js_slice(h, 0, 50)}"' for h in f["skipped"])
                    early_warnings.append(f"Jev skipped {len(f['skipped'])} of {len(sections)} sections as side material: {skipped}.")
                if f.get("note"):
                    early_warnings.append(f"Section filter: {f['note']}.")
                _step(emit, "read", "done", f"kept {len(kept)} of {len(sections)} sections (Jev){ref_note}")
                groups = group_sections(kept)
                _step(emit, "summarize", "running", f"0 / {len(groups)} parts")
                done = [0]
                lock = threading.Lock()

                def summ(g: str) -> str:
                    s = summarize(client, g, usage)
                    with lock:
                        done[0] += 1
                        n = done[0]
                    _step(emit, "summarize", "running", f"{n} / {len(groups)} parts")
                    return s

                summaries = list(pool.map(summ, groups))
                useful = [s for s in summaries if s and not s.startswith("(no content)")]
                _step(emit, "summarize", "done", f"{len(groups)} parts")
                # Only a full read is cached: a filter that fell back (no Jev key, Jev errors) or empty summaries aren't.
                if paper_cache is not None and not f.get("note") and useful:
                    paper_cache.put(key, {"skipped": f["skipped"], "kept": len(kept), "groups": len(groups), "summaries": useful})
            source_context = "\n\n".join(useful)
            request = "\n\n".join(x for x in [
                f'Create an interactive explainer for this AI research paper: "{req["title"]}".',
                f"The reader's note: {req['note']}" if req["note"] else "",
                hint,
                brief_line(),
                continuation_note(follow) if follow and follow["kind"] == "new" else "",
                f"Section summaries:\n\n{source_context}",
            ] if x)
        elif adding:
            _step(emit, "plan", "running", "Jev is choosing where the new section goes")
            place = place_section(jev_key, old_outline, req["text"], usage, early_warnings)
            request = "\n\n".join(x for x in [addition_request(req["text"], follow, old_outline, place), hint] if x)
            source_context = follow.get("source") or f"Reader's request: {req['text']}"
        elif follow and follow["kind"] == "revise" and follow.get("improve") and old_outline is not None:
            # Quality 2.0's improve step (deepen.py): the audit's fix list, after the page block the audit cached.
            request = improve_request(follow, CHANGE_RULES, hint)
            source_context = follow.get("source") or f"Reader's request: {req['text']}"
        elif follow and follow["kind"] == "revise":
            # A new version of the explainer above, planned from its outline (no paper re-read).
            request = "\n\n".join(x for x in [revision_request(req["text"], follow, changes=old_outline is not None), hint, brief_line()] if x)
            source_context = follow.get("source") or f"Reader's request: {req['text']}"
        else:
            request = "\n\n".join(x for x in [f"Create an interactive explainer for this AI concept request:\n\n{req['text']}", hint, brief_line(),
                                              continuation_note(follow) if follow else ""] if x)
            source_context = "\n\n".join(x for x in [f"Reader's request: {req['text']}", (follow or {}).get("source") or ""] if x)

        brief = brief_future.result() if brief_future else None

    if adding:
        plan_detail = place_detail(place)
    elif follow:
        plan_detail = "Revising the previous version" if follow["kind"] == "revise" else "Building on the conversation"
    else:
        plan_detail = f"Jev: {brief_summary(brief)}" if brief else ""
    _step(emit, "plan", "running", plan_detail or None)
    revision = None  # set when a revision came back as changes: {"changed": [stage ids], "removed": n}
    smart_picks: list[dict] = []
    smart_widgets = 0
    # A revision that is about one part of the page (targets.py): the finder says which parts, and only those are changed.
    # None = the old path (the whole page is planned again from the outline): a page-wide request, "Add section", the
    # review rounds' improve, Lab, or a finder that failed.
    targeted = None
    # Feedback v2 (feedback.py): the parts the reader confirmed. They replace the finder, and a change that can't be matched
    # to the page is an error, never a silent whole-page rewrite. pageWide: the reader chose to change the whole page.
    fb = follow.get("feedback") if follow and follow["kind"] == "revise" else None
    targetable = (base_outline is not None and follow and follow["kind"] == "revise" and not adding and not follow.get("add")
                  and not follow.get("improve") and not scenes and not smart)
    if fb and not fb["pageWide"] and not targetable:
        raise ReaderError("This page can only be changed as a whole. Send the feedback again and choose \"The whole page\".")
    if targetable and fb and not fb["pageWide"]:
        _step(emit, "plan", "running", "Applying your feedback")
        known = restore_kept_widgets(base_outline, follow["widgets"]) if follow.get("widgets") else base_outline
        keep = set(fb["keep"])
        found = clean_targets({"targets": [t for t in fb["targets"] if t["part"] == "new-section" or t["stage"] not in keep]}, known)
        if not found or not found["targets"]:
            raise ReaderError("This feedback couldn't be matched to the page, so nothing was changed. Send it again and pick the part you mean.")
        targeted = found["targets"]
        _step(emit, "plan", "running", "Changing " + (f"{len(targeted)} parts" if len(targeted) > 1 else "one part") + " of the page")
    elif targetable and not fb:
        _step(emit, "plan", "running", "Deciding what to change")
        known = restore_kept_widgets(base_outline, follow["widgets"]) if follow.get("widgets") else base_outline
        found = find_targets(client, usage, known, req["text"])
        targeted = found["targets"] if found and found["targets"] else None
        if targeted:
            _step(emit, "plan", "running", "Changing " + (f"{len(targeted)} parts" if len(targeted) > 1 else "one part") + " of the page")
    if smart:  # sections, Jev's style per section, then the Quality+ planner; widgets only where Jev picked them
        sections = smart_mode.plan_sections(client, request, usage, asked["count"] if asked else None)
        if sections:
            _step(emit, "plan", "running", "Jev is choosing a style for each section")
            level = (brief or {}).get("level") or req.get("level")
            title = req["title"] if req["mode"] == "paper" else js_slice(req["text"], 0, 200)
            smart_picks = smart_mode.pick_styles(jev_key, title, level, sections, usage, early_warnings)
            plan_request = f"{request}\n\n{smart_mode.smart_directive(sections, smart_picks)}"
            _step(emit, "plan", "running", smart_mode.detail(smart_picks))
        else:
            early_warnings.append("Smart mode: the section plan failed, so the page was written as in Interactive.")
            plan_request = request
        lab = write_outline(client, plan_request, emit, usage, _prompt("OUTLINE_SYSTEM"), parse_lab_outline, effort)
        warnings = lab["warnings"]
        lab_outline = smart_mode.enforce(lab["outline"], smart_picks, warnings) if smart_picks else lab["outline"]
        if not smart_picks:  # no plan, no picks: no custom widgets either
            lab_outline = {**lab_outline, "stages": [{**st, "blocks": [b for b in st["blocks"] if b["type"] != "interactive"]} for st in lab_outline["stages"]]}
        _step(emit, "plan", "done", f"{len(lab_outline['stages'])} stages" + (f" · {smart_mode.detail(smart_picks)}" if smart_picks else ""))
        if briefs_of(lab_outline):
            bundles = build_widgets(client, lab_outline, usage, emit, warnings, fill_effort)
            smart_widgets = sum(1 for b in bundles.values() if any(x["type"] == "widget" for x in b))
            _step(emit, "components", "done", f"{smart_widgets} custom simulation{'' if smart_widgets == 1 else 's'}")
        else:
            bundles = {}
        outline = merge_bundles(lab_outline, bundles)
        if brief and brief.get("level") and outline["level"] != brief["level"]:
            outline = {**outline, "level": brief["level"]}
    elif freeform and not targeted:  # briefs, then Haiku writes each widget's code
        lab = write_outline(client, request, emit, usage, _prompt("LAB_SYSTEM"), parse_lab_outline, effort)
        _step(emit, "plan", "done", f"{len(lab['outline']['stages'])} stages, {len(briefs_of(lab['outline']))} interactive parts")
        warnings = lab["warnings"]
        bundles = build_widgets(client, lab["outline"], usage, emit, warnings, fill_effort)
        built = sum(1 for b in bundles.values() if any(x["type"] == "widget" for x in b))
        _step(emit, "components", "done", f"{built} widgets, {len(bundles) - built} as text")
        outline = merge_bundles(lab["outline"], bundles)
    else:
        system = _prompt("LITE_SYSTEM") if lite else _prompt("OUTLINE_SYSTEM") if code_lab else planner_system(force_chart=scenes)
        # Code Lab: the planner is told in the user turn (the cached system prompt stays shared) to write prose only.
        lab_note = PLANNER_NOTE_PG if lab_settings.playground else PLANNER_NOTE_4 if lab_settings.marks else PLANNER_NOTE
        plan_text = f"{request}\n\n{lab_note}" if code_lab else request
        streamed = None  # Code Lab, new page only: widgets start while the text streams
        if targeted:
            # A Code Lab 7.0 or Playground page: its interactive parts are widgets, so the planner writes explanation only.
            prose = page_kind(restore_kept_widgets(base_outline, follow.get("widgets") or {}), req.get("quality")) is not None
            revise_system = _prompt("OUTLINE_SYSTEM") if prose else planner_system()

            def ask(text: str, parse: Callable[[Any], dict]) -> dict:
                return write_outline(client, text, emit, usage, revise_system, parse, effort, whole="your reply")
            def run_targets(ts: list[dict]) -> dict:
                return apply_targets(client, usage, jev_key, follow, base_outline, ts, req["text"], ask, effort, req.get("planThinking", True), req.get("quality"), prose)
            r = run_targets(targeted)
            if fb:  # is each changed part better than before? (feedback_check.py)
                _step(emit, "plan", "running", "Checking the changes")
                r = feedback_check.check(client, usage, jev_key, follow, base_outline, r, targeted, req["text"], ask, run_targets, fb["id"])
                if not (r["revision"]["changed"] or r["revision"].get("widgets")):  # never a new version identical to the last
                    why = next((w for w in r["warnings"] if w.startswith(("Couldn't", "Kept the previous"))), "")
                    raise ReaderError("Nothing on the page was changed" + (f": {why}" if why else ".") + " This didn't count as one of today's explainers.")
        elif adding:
            r = write_outline(client, request, emit, usage, system, lambda raw: parse_addition(raw, old_outline, place["after"]), effort, whole="your reply")
        elif old_outline is not None:
            r = write_outline(client, request, emit, usage, system, lambda raw: parse_revision(raw, old_outline), effort, whole="your reply")
        else:
            streamed = Streamed(client, usage, jev_key, lab_settings) if code_lab else None
            r = write_outline(client, plan_text, emit, usage, system, parse_outline, effort, "compact" if lite else "json", LITE_BUDGET if lite else FULL_BUDGET,
                              on_text=streamed.feed if streamed else None)
        outline, warnings, revision = r["outline"], r["warnings"], r.get("revision")
        # The brief's level wins over the planner's, so the page badge and the chatbot match it.
        if brief and brief.get("level") and outline["level"] != brief["level"]:
            outline = {**outline, "level": brief["level"]}
        # Lite+: strip any question, example or click-to-use block the planner wrote anyway.
        if lite:
            outline = without_blocks(outline, set(data.constants()["LITE_DROPPED"]))
        # "None" (or any answer without worked examples): strip any the planner wrote anyway.
        if drops_examples(req.get("extras")):
            outline = without_blocks(outline, {"examples"})
        # Chart block on: numeric experiments the planner still wrote become charts (Jev picks the view).
        if not lite and not code_lab and not targeted and (chart_on() or scenes):
            outline = experiments_to_charts(outline, jev_key, usage, warnings)
        if code_lab:  # Haiku's prose only; Haiku then writes the interactions (started while the text streamed, for a new page)
            outline = strip_prebuilt(outline)
            _step(emit, "interaction", "running")
            if streamed:
                outline, cl = streamed.finish(outline, warnings, lambda d: _step(emit, "interaction", "running", d))
            else:
                outline, cl = design_code_widgets(client, outline, usage, warnings, jev_key=jev_key, settings=lab_settings)
            rejected = cl.get("rejected") or []
            lab_marks = cl.get("marks") or {}
            lab_math = cl.get("math") or []
            lab_look = cl.get("look") or []
            _step(emit, "interaction", "done", f"{cl['built']} of {cl['sections']} sections got a " + ("playground" if lab_settings.playground else "custom interaction") + (f" · {cl['charts']} as charts" if cl.get("charts") else "") + (f" · {cl['repaired']} repaired" if cl["repaired"] else ""))
        if scenes:
            _step(emit, "interaction", "running")
            outline, labs = design_scenes(client, outline, usage, warnings, jev_key=jev_key)
            _step(emit, "interaction", "done", f"{labs['opened']} of {labs['sections']} sections chosen · {labs['placed']} connected lab{'' if labs['placed'] == 1 else 's'}")
        # A revision saw each widget as a placeholder; put back the ones it kept (before the checks, which skip widgets).
        if follow and follow["kind"] == "revise" and follow.get("widgets"):
            outline = restore_kept_widgets(outline, follow["widgets"])
        if revision is not None:
            _step(emit, "plan", "done", revision_summary(revision, outline, base_outline)["text"])
        else:
            _step(emit, "plan", "done", f"{len(outline['stages'])} stages")

    _step(emit, "design", "running")
    if follow and follow["kind"] == "revise":
        # A revision keeps the previous version's look (and saves the design call).
        design = follow["design"]
        _step(emit, "design", "done", "kept from the previous version")
    else:
        design = choose_design(client, outline, request, usage)
        if brief:
            design = design_with_brief(design or default_design(outline), brief)
        jev_pick = " (Jev)" if brief and (brief.get("theme") or brief.get("fontPairing")) else ""
        _step(emit, "design", "done", f"{design['theme']} · {design['fontPairing']}{jev_pick}" if design else "default design")

    _step(emit, "build", "running")
    context = js_slice(f"{outline_context(outline)}\n\nSource material:\n{source_context}", 0, CONTEXT_CHARS)
    spec = assemble_spec(outline, design, context, [*early_warnings, *warnings])
    if lite:
        spec = {**spec, "noAsk": True, "noControls": True}
    if smart_picks:
        spec = {**spec, "smart": smart_mode.summary(smart_picks, smart_widgets)}
    tools = section_tools(req.get("extras"))
    if tools:
        spec = {**spec, "sectionTools": tools}
    # No paper text behind it (a follow-up may carry an earlier paper's), so its quoted figures are recalled, not read.
    if req["mode"] == "concept" and not (follow and follow.get("source")):
        spec = {**spec, "fromMemory": True}
    if brief and not (follow and follow["kind"] == "revise"):
        spec = apply_brief(spec, brief)
    if follow and follow["kind"] == "revise" and (follow.get("accent") or follow.get("colorMode")):
        spec = apply_prefs(spec, {"palette": "custom" if follow.get("accent") else follow["design"]["theme"], "accent": follow.get("accent"), "colorMode": follow.get("colorMode") or "auto"})
    # Page colours chosen in the composer win over the previous version's: the page opens and exports in it.
    if req.get("colorMode") in ("light", "dark"):
        spec = {**spec, "design": {**spec["design"], "mode": req["colorMode"]}}
    # A revision made as changes: unchanged stages keep what the previous version wrote for them, and the
    # later steps run only on the changed stages (`only`, indices into spec["stages"]).
    only = None
    kept_notes = None
    if revision is not None:
        changed = set(revision["changed"])
        only = [i for i, st in enumerate(spec["stages"]) if st["id"] in changed]
        spec = {**spec, "revision": revision_summary(revision, outline, base_outline)["counts"]}
        if adding:  # where Jev put it, so placements can be judged before trusting them
            spec["addition"] = {"after": place["label"], "p": round_num(place["p"], 2), "placed": place["placed"],
                                **({"covered": place["covered"]} if place.get("covered") else {})}
        kept = follow.get("kept") or {}
        steps = {k: v for k, v in (kept.get("steps") or {}).items() if k.rsplit(":", 2)[0] not in changed}
        if steps:
            spec = apply_example_steps(spec, steps)
        kept_notes = {sid: n for sid, n in (kept.get("notes") or {}).items() if sid not in changed}
    _step(emit, "build", "done")

    if example_briefs(spec):
        _step(emit, "examples", "running")
        spec = write_examples(client, spec, usage)
        _step(emit, "examples", "done")
    if checks and quality in ("quality-plus", "freeform-plus", "smart"):
        # Jev judges each stage (unexplained ideas, too hard), Haiku fixes what it flags (clarity.py).
        # It runs before the fact check, so the text it adds is fact-checked too. Both only read and edit
        # the prose; Freeform+ widgets are AI-written code, never edited, and claims_of skips them.
        _step(emit, "clarity", "running")
        spec, detail = clarity_check(client, jev_key, spec, usage, only=only)
        _step(emit, "clarity", "done", detail)
        # Jev flags statements that look wrong, Haiku fixes the ones that are (factcheck.py).
        _step(emit, "check", "running")
        spec, detail = fact_check(client, jev_key, spec, usage, section_check=FACTCHECK_SECTIONS, only=only)
        _step(emit, "check", "done", detail)
    # "Add to each section": deeper explanations / other wording, written last so they build on the checked text.
    if note_fields(req.get("extras")):
        _step(emit, "notes", "running")
        spec, detail = write_notes(client, spec, req.get("extras"), usage, kept=kept_notes)
        _step(emit, "notes", "done", detail)
    return {"spec": spec, "usage": usage.summary(), **({"rejected": rejected} if rejected else {}), **({"labMarks": lab_marks} if lab_marks else {}), **({"labMath": lab_math} if lab_math else {}), **({"labLook": lab_look} if lab_look else {})}
