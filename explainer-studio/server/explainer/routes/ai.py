"""The AI endpoints (all signed in; each counts toward the user's daily allowance, quota.py):

  POST /api/generate  -> SSE stream of GenerateEvent (progress, then the spec). With "recordId", the server
                         also saves progress and the result to that history record (history.py), so a
                         generation finishes even if the tab is closed.
  POST /api/chat      -> SSE stream of {t: text} deltas, then {done: true}
  POST /api/edit      -> JSON {content, usd}: one Edit chat turn (Haiku picks tools; the browser runs them)
  POST /api/route     -> JSON {kind, usd}: does a follow-up revise the explainer above or ask for a new one?
  POST /api/section   -> JSON {md, sources, searches, usd}: a section tool the reader clicked (web search)
  POST /api/formula   -> JSON {note, usd}: "Go deeper" on a formula (formula.py); the app saves the note on the page
  POST /api/feedback  -> JSON {feedbackId, plan, preference, usd}: the Feedback button (feedback.py), a plan the
                         reader confirms
  POST /api/feedback/<id>/confirm -> JSON {feedback, preference}: no AI call; the app then runs the confirmed
                         change as a revise follow-up carrying FollowUp.feedback
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from flask import Blueprint, Response, current_app, g
from limits import parse as parse_limit

from .. import data, feedback, history, quota, sse
from ..edit_chat import edit_turn, parse_edit
from ..followup import parse_route, route_follow_up
from ..formula import formula_turn, parse_formula
from ..llm import HAIKU, friendly_error
from ..middleware import ApiError, ctx, json_body, json_response, rate_limit, require_auth
from ..deepen import generate_deep
from ..pipeline import generate
from ..pricing import UsageLog, price_of
from ..requests import parse_chat, parse_generate
from ..schemas import RECORD_ID
from ..section import parse_section, section_turn

log = logging.getLogger("explainer")
bp = Blueprint("ai", __name__, url_prefix="/api")

# Per-user caps: generations, and chat/edit/route/section messages together.
GEN_LIMIT = parse_limit("3/minute")
CHAT_LIMIT = parse_limit("20/minute")

# Modes the app no longer offers (CLAUDE.md: Smart has no button, Lab and Code Lab 1-3 are hidden). The code is
# kept, but only test scripts may run them, with ALLOW_HIDDEN_MODES=1; anyone else gets Quality+ (audit F09).
HIDDEN_MODES = ("smart", "scene-plus", "code-lab", "code-lab-2", "code-lab-3")
DEBUG_FLAGS = ("debugVersions", "debugRejected")  # scripts/variety.mjs only, with ALLOW_DEBUG=1 (audit F10)


class GenerationCancelled(BaseException):
    """The record (or account) was deleted while its generation ran (audit F18). A BaseException, so the
    pipeline's own `except Exception` fallbacks let it through and the run stops at its next step."""


def _policy(ai: dict[str, Any], body: Any, req: dict) -> dict:
    """What the server allows of a parsed GenerateRequest, whatever the client sent."""
    if req["quality"] in HIDDEN_MODES and not ai.get("allow_hidden_modes"):
        req = parse_generate({**body, "quality": "quality-plus"})  # re-parsed, so mode-only options are dropped
        if isinstance(req, str):
            raise ApiError(400, req, "invalid")
    if not ai.get("allow_debug"):
        req = {k: v for k, v in req.items() if k not in DEBUG_FLAGS}
    return req


def _ai() -> dict[str, Any]:
    """The Anthropic client and AI settings (made once in create_app)."""
    ai = current_app.extensions["explainer_ai"]
    if ai["client"] is None:
        raise ApiError(500, "Server is missing ANTHROPIC_API_KEY.", "misconfigured")
    return ai


def _sse(work: Callable) -> Response:
    return Response(sse.stream(work), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _parsed(parse: Callable[[Any], Any], body: Any = None) -> Any:
    """The parsed request, or a 400. body: the JSON body when the route already read it (it needs more of it)."""
    req = parse(json_body() if body is None else body)
    if isinstance(req, str):
        raise ApiError(400, req, "invalid")
    return req


@bp.post("/generate")
@require_auth
def generate_route() -> Response:
    ai = _ai()
    c = ctx()
    user_id = g.user.id
    rid = g.get("request_id")  # the stream runs in a thread, without the request context
    rate_limit(GEN_LIMIT, "gen", user_id, "Too many explainers at once. Please wait a minute.")
    body = json_body()
    req = _parsed(parse_generate, body)
    req = _policy(ai, body, req)
    # The reader's saved preferences (feedback.py), loaded here so only the server can set them; the pipeline adds
    # them to the planner's hint. None saved: nothing changes.
    if feedback.prefs_enabled():
        prefs = feedback.active_prefs(c.db, user_id)
        if prefs:
            req = {**req, "prefs": prefs}
    record_id = body.get("recordId") if isinstance(body, dict) else None
    if record_id is not None and (not isinstance(record_id, str) or not RECORD_ID.match(record_id)):
        raise ApiError(400, "Invalid record id.", "invalid")
    event_id = quota.start_generation(c.settings, c.db, user_id)
    try:
        budget = quota.remaining_usd(c.settings, c.db, user_id)
        if record_id:
            history.start_generation(c.db, user_id, record_id)
    except Exception:
        # The run never starts, so it mustn't count as one of today's or stay "running" (audit F12).
        quota.finish_generation(c.db, event_id, 0.0, failed=True)
        raise

    def work(send) -> None:
        def emit(e: dict) -> None:
            if record_id and not history.generation_event(c.db, user_id, record_id, e):
                raise GenerationCancelled()
            send(e)

        # Made here, not in the pipeline, so a run that fails partway still has its spend counted.
        usage = UsageLog("")
        usd, failed = 0.0, True
        try:
            # Review rounds (deepen.py): quality-deep, or any mode with reviewRounds. They are where one run can cost
            # many times the allowance, so they stop once today's budget is spent (audit F07).
            deep = req["quality"] == "quality-deep" or req.get("reviewRounds")
            kw = {"jev_key": ai["jev_key"], "plan_effort": ai["plan_effort"], "paper_cache": ai["paper_cache"], "usage": usage}
            r = generate_deep(ai["client"], req, emit, usd_budget=budget, **kw) if deep else generate(ai["client"], req, emit, **kw)
            u = r["usage"]
            usd, failed = u["totalUsd"], False
            cached = sum(s["cacheRead"] for s in u["steps"])
            fresh = sum(s["input"] + s["cacheWrite"] for s in u["steps"])
            log.info('generated "%s" (%s, %s effort) $%.4f in %ss; prompt cache %d%% of input', r["spec"]["title"], u["quality"], u["effort"],
                     usd, u["seconds"], round(100 * cached / max(1, cached + fresh)))
            emit({"type": "done", "spec": r["spec"], "usage": u, **({"versions": r["versions"]} if req.get("debugVersions") and "versions" in r else {}),
                  **({"rejected": r["rejected"]} if req.get("debugRejected") and r.get("rejected") else {}),
                  **({"labMarks": r["labMarks"]} if req.get("debugRejected") and r.get("labMarks") else {}),
                  **({"labMath": r["labMath"]} if req.get("debugRejected") and r.get("labMath") else {}),
                  **({"labLook": r["labLook"]} if req.get("debugRejected") and r.get("labLook") else {})})
        except GenerationCancelled:
            usd = usage.summary()["totalUsd"]  # what was spent until it stopped still counts
            log.info("generation for deleted record %s stopped after $%.4f", record_id, usd)
            send({"type": "error", "message": "This explainer was deleted, so its generation was stopped."})
        except Exception as e:
            usd = usage.summary()["totalUsd"]
            log.exception("generate failed after $%.4f", usd)
            try:
                emit({"type": "error", "message": friendly_error(e, rid)})
            except GenerationCancelled:
                pass
        finally:
            quota.finish_generation(c.db, event_id, usd, failed=failed)

    return _sse(work)


def _chat_turn(kind: str, message: str, run: Callable[[Any], dict], parse: Callable[[Any], Any]) -> Response:
    """A one-shot JSON call (edit, route, section, formula): limits, allowance, then the reply with its cost recorded."""
    ai = _ai()
    c = ctx()
    rate_limit(CHAT_LIMIT, "chat", g.user.id, message)
    req = _parsed(parse)
    quota.check_spend(c.settings, c.db, g.user.id)
    try:
        r = run(ai["client"], req)
    except Exception as e:
        log.exception("%s failed", kind)
        # A turn that failed after a paid reply (an unusable answer, a later search call) says what it spent (audit F23).
        spent = getattr(e, "spent_usd", 0.0) or 0.0
        if spent > 0:
            quota.record(c.settings, c.db, g.user.id, kind, spent)
        raise ApiError(502, friendly_error(e), "upstream_error") from None
    quota.record(c.settings, c.db, g.user.id, kind, r.get("usd") or 0.0)
    if kind == "section":
        log.info("section %s: %d searches, $%.4f", req["kind"], r["searches"], r["usd"])
    return json_response(r)


@bp.post("/route")
@require_auth
def route_route() -> Response:
    return _chat_turn("route", "Too many messages at once. Please wait a minute.", route_follow_up, parse_route)


@bp.post("/edit")
@require_auth
def edit_route() -> Response:
    return _chat_turn("edit", "Too many edits at once. Please wait a minute.", edit_turn, parse_edit)


@bp.post("/section")
@require_auth
def section_route() -> Response:
    return _chat_turn("section", "Too many requests at once. Please wait a minute.", section_turn, parse_section)


@bp.post("/formula")
@require_auth
def formula_route() -> Response:
    """"Go deeper" on a formula (formula.py): one Haiku call on the click; the app saves the note on the page."""
    return _chat_turn("formula", "Too many requests at once. Please wait a minute.", formula_turn, parse_formula)


@bp.post("/feedback")
@require_auth
def feedback_route() -> Response:
    """The Feedback button, step 1: one Haiku call that turns the feedback into a plan for the reader to confirm.
    Nothing is saved for the reader yet (see /feedback/<id>/confirm)."""
    db, user_id = ctx().db, g.user.id
    return _chat_turn("feedback", "Too many messages at once. Please wait a minute.",
                      lambda client, req: feedback.handle(client, db, user_id, req), feedback.parse_feedback)


@bp.post("/feedback/<feedback_id>/confirm")
@require_auth
def feedback_confirm_route(feedback_id: str) -> Response:
    """The Feedback button, step 2 (no AI call): the change the reader confirmed, and their preference when "Remember" is
    ticked. Returns {feedback: FollowUp.feedback | null, preference | null}; the app then runs the change."""
    c = ctx()
    rate_limit(CHAT_LIMIT, "chat", g.user.id, "Too many messages at once. Please wait a minute.")
    try:
        r = feedback.confirm(c.db, g.user.id, feedback_id, json_body())
    except ValueError as e:
        raise ApiError(409, str(e), "used") from None
    if r is None:
        raise ApiError(404, "Not found.")
    return json_response(r)


def chat_system(context: str) -> list[dict]:
    """The chatbot's system blocks, built on the server only (audit F03): fixed rules first, then the
    client's explainer context wrapped in tags as data. A closing tag inside it can't end the block early."""
    context = re.sub(r"<\s*/?\s*explainer_context\s*>", "[explainer context tag removed]", context, flags=re.I)
    return [
        {"type": "text", "text": f"{data.prompt('CHAT_SYSTEM')}\n\n{data.prompt('CHAT_RULES')}"},
        # The explainer's context is the same on every question about it: cached.
        {"type": "text", "text": f"<explainer_context>\n{context}\n</explainer_context>", "cache_control": {"type": "ephemeral"}},
    ]


@bp.post("/chat")
@require_auth
def chat_route() -> Response:
    ai = _ai()
    c = ctx()
    user_id = g.user.id
    rid = g.get("request_id")  # the stream runs in a thread, without the request context
    rate_limit(CHAT_LIMIT, "chat", user_id, "Too many questions. Please wait a minute.")
    req = _parsed(parse_chat)
    quota.check_spend(c.settings, c.db, user_id)
    client = ai["client"]

    def chat(send) -> None:
        opened = None  # the stream, once it started: a stream that fails partway was still billed (audit F23)
        recorded = False
        try:
            with client.messages.stream(
                model=HAIKU,
                max_tokens=4000,
                output_config={"effort": "low"},
                system=chat_system(req["context"]),
                messages=req["messages"],
            ) as stream:
                opened = stream
                for t in stream.text_stream:
                    send({"t": t})
                msg = stream.get_final_message()
            recorded = True
            quota.record(c.settings, c.db, user_id, "chat", price_of(msg.model, msg.usage))
            if msg.stop_reason == "refusal":
                send({"t": "\n\n_Sorry, I can't help with that one._"})
            send({"done": True})
        except Exception as e:
            log.exception("chat failed")
            if opened is not None and not recorded:
                try:
                    snap = opened.current_message_snapshot
                    quota.record(c.settings, c.db, user_id, "chat", price_of(snap.model, snap.usage))
                except Exception:
                    log.exception("couldn't record the spend of a failed chat")
            send({"error": friendly_error(e, rid)})

    return _sse(chat)
