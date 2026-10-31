"""Feedback on a finished page, and the reader's saved preferences.

Feedback never guesses which part to change:
1. POST /api/feedback (understand):
   a. Code builds a summary card of every section from the page's JSON (feedback_pick.section_cards: its parts, each
      widget's controls and what it shows, and what the previous feedback changed). No AI call.
   b. A small Haiku call reads the feedback and the cards and decides which sections it asks to change, also when the
      reader describes a kind of part instead of naming it (feedback_pick.pick_sections). decide_scope turns that into
      what the plan step may change: those sections, a new section, the whole page, nothing, or "ask the reader" about
      the sections it isn't sure of.
   c. A second Haiku call (FEEDBACK_SYSTEM) gets all the cards plus the full JSON of only those sections, and writes the
      plan: the changes (each tied to a quote of the reader's words), the sections to keep, or a question whose answers
      are concrete changes to the unsure sections. When the pick is unusable it picks among all sections itself.
   Jev is only used after the edit (feedback_check.py).
   Code then checks the plan (clean_plan): changes outside the scope, unknown sections, unquoted changes and changes to
   kept sections are dropped; a widget added past the page's limit becomes a change to the section's own blocks; and when
   nothing usable is left, the code asks "Which part did you mean?" itself. Nothing is saved for the reader yet.
2. The app shows the plan; the reader ticks, edits or answers. "Something else" sends the answer back (round 2, which never
   asks again).
3. POST /api/feedback/<id>/confirm: saves what the reader confirmed (and the preference, when "Remember" is ticked). The app
   then runs the change as a revise follow-up carrying FollowUp.feedback: the pipeline edits only those parts (no target
   finder, no saved preferences, no silent whole-page rewrite) and feedback_check.py checks each changed part.

Active preferences are added to the planner's hint for this reader's later new pages (prefs_hint; routes/ai.py loads them on
the server, so a client can't add any). PREFERENCES=0 turns that off. Nobody else's pages see them.
"""

from __future__ import annotations

import os
import re
from typing import Any

import anthropic
from sqlalchemy import select

from . import data
from . import feedback_pick as fp
from .codelab import MAX_MARKED
from .codelab_edit import read_plan as ce_plan
from .db import Database, Feedback, UserPreference, now_ms
from .followup import FEEDBACK_ID, parse_follow_up
from .js import js_len, js_slice, js_trim, json_parse, json_stringify
from .llm import HAIKU, cached_system, text_of
from .pricing import price_of
from .revise import restore_kept_widgets
from .schemas import RECORD_ID
from .targets import MAX_TARGETS, clean_targets

TEXT_MAX = 1000
ANSWER_MAX = 300
PREF_MAX = 200
WHAT_MAX = 300
LABEL_MAX = 60
MAX_ACTIVE = 8  # active preferences per reader; a new one past this retires the oldest
MAX_OPTIONS = 12  # question chips, including "The whole page"
TOPICS = ("visuals", "controls", "level", "length", "tone", "other")
STATUSES = ("active", "off")  # what the reader can set; "replaced" is the server's
WHOLE_PAGE = "The whole page"


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _clean(s: str, n: int) -> str:
    return js_slice(" ".join(s.split()), 0, n)


def parse_feedback(body: Any) -> dict | str:
    body = body if isinstance(body, dict) else {}
    text = js_trim(_str(body.get("text")))
    if not text:
        return "Write your feedback."
    if js_len(text) > TEXT_MAX:
        return f"Feedback can be at most {TEXT_MAX} characters."
    record_id = body.get("recordId")
    if record_id is not None and (not isinstance(record_id, str) or not RECORD_ID.match(record_id)):
        return "Invalid record id."
    follow = parse_follow_up(body.get("followUp"))
    if not isinstance(follow, dict) or follow["kind"] != "revise":
        return follow if isinstance(follow, str) else "The page couldn't be read for feedback."
    answer = js_trim(_str(body.get("answer")))
    if js_len(answer) > ANSWER_MAX:
        return f"Your answer can be at most {ANSWER_MAX} characters."
    warnings = [_clean(w, 300) for w in body.get("warnings") if isinstance(w, str) and w.strip()][:20] if isinstance(body.get("warnings"), list) else []
    # What the feedback that made this version changed ({stages, what}), so "the play button" can mean the one just added.
    last = body.get("lastChange") if isinstance(body.get("lastChange"), dict) else {}
    last_stages = [js_slice(s, 0, 80) for s in last.get("stages") if isinstance(s, str)][:6] if isinstance(last.get("stages"), list) else []
    last_what = _clean(_str(last.get("what")), 400)
    return {"text": text, "recordId": record_id, "title": _clean(_str(body.get("title")), 200), "warnings": warnings,
            "followUp": follow, "answer": answer, "round": 2 if body.get("round") == 2 and answer else 1,
            "lastChange": {"stages": last_stages, "what": last_what} if last_stages and last_what else None,
            # Lab (scene-plus) pages can't be changed part by part (pipeline.py), only as a whole.
            "wholeOnly": body.get("quality") == "scene-plus"}


# ---------- understanding the feedback ----------


def page_of(follow: dict) -> dict:
    """The page as the follow-up carries it, with its real widgets back (for the digest and the stage ids)."""
    o = json_parse(follow["outline"])
    return restore_kept_widgets(o, follow["widgets"]) if follow.get("widgets") else o


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def quoted(quote: str, source: str) -> bool:
    """Whether a change's quote really comes from the reader's words (source, already _norm'ed): the quote is in them, or
    nearly all its words are (Haiku sometimes trims a word or fixes a typo)."""
    q = _norm(quote)
    if not q:
        return False
    if q in source:
        return True
    words = [w for w in q.split() if len(w) > 2]
    have = set(source.split())
    return bool(words) and sum(w in have for w in words) >= 0.75 * len(words)


def _one_target(t: dict, page: dict) -> dict | None:
    r = clean_targets({"targets": [t]}, page)
    return r["targets"][0] if r and r["targets"] else None


def _labels(page: dict) -> dict[str, str]:
    return {st["id"]: str(st.get("label") or st["id"]) for st in page["stages"]}


def _with_label(t: dict, labels: dict[str, str]) -> dict:
    if t["part"] == "new-section":
        where = f"New section after {labels[t['after']]}" if t.get("after") in labels else "New section at the start"
    else:
        where = labels.get(t["stage"], t["stage"])
    return {**t, "what": js_slice(t["what"], 0, WHAT_MAX), "stageLabel": where}


def _has_widget(st: dict) -> bool:
    return any(b.get("type") == "widget" for b in st.get("blocks") or [])


def _title_words(title: str) -> set[str]:
    return {w for w in _norm(title).split() if len(w) > 2}


def place_widget_changes(changes: list[dict], page: dict, source: str) -> list[dict]:
    """A "widget" change on a section without a widget means "add one". When the reader's words name an existing widget by
    its title (all its longer words appear) and only one section has it, the change goes to that section instead."""
    words = set(source.split())
    named = [st["id"] for st in page["stages"] for b in st["blocks"]
             if b.get("type") == "widget" and _title_words(str(b.get("title") or "")) and _title_words(str(b.get("title") or "")) <= words]
    by_id = {st["id"]: st for st in page["stages"]}
    out = []
    for t in changes:
        if t["part"] == "widget" and not _has_widget(by_id.get(t["stage"], {})) and len(set(named)) == 1:
            t = {**t, "stage": named[0]}
        out.append(t)
    return out


def widget_count(page: dict) -> int:
    return sum(1 for st in page["stages"] for b in st.get("blocks") or [] if b.get("type") == "widget")


def fit_widget_limit(changes: list[dict], page: dict) -> list[dict]:
    """On a page with widgets, a "widget" change on a section without one adds a widget, which is refused past MAX_MARKED.
    Then it becomes a "text" change instead: the section's own charts, sliders and bars are changed (they are ordinary
    blocks), so the change still happens."""
    if not widget_count(page) or widget_count(page) < MAX_MARKED:
        return changes
    by_id = {st["id"]: st for st in page["stages"]}
    return [{**t, "part": "text"} if t["part"] == "widget" and not _has_widget(by_id.get(t["stage"], {})) else t for t in changes]


def which_part_question(page: dict, text: str, ids: list[str] | None = None) -> dict:
    """The code's own question when it can't tell which part is meant: one chip per section (and per interactive part) of
    `ids` (best first; every section when None), then the whole page. Each chip carries the reader's words as the change."""
    what = f'Make the change the reader asked for: "{js_slice(" ".join(text.split()), 0, 240)}"'
    options = []
    stages = page["stages"] if not ids else [st for sid in ids for st in page["stages"] if st["id"] == sid]
    for st in stages:
        label = js_slice(str(st.get("label") or st["id"]), 0, LABEL_MAX)
        options.append({"label": label, "part": "text", "stage": st["id"], "after": "", "what": what})
        if _has_widget(st):
            options.append({"label": js_slice(f"{label} · interactive part", 0, LABEL_MAX), "part": "widget", "stage": st["id"], "after": "", "what": what})
    options = options[:MAX_OPTIONS - 1] + [{"label": WHOLE_PAGE, "part": "page", "stage": "", "after": "", "what": ""}]
    return {"text": "Which part did you mean?", "options": options}


ANY_SCOPE = {"allowed": None, "allowNew": True, "ask": None, "pageWide": False, "needsChange": False, "work": []}


def decide_scope(pick: dict | None, page: dict, req: dict) -> dict:
    """What the writer may change, from the pick (feedback_pick.decide):
      allowed     stage ids it may change (None = any: the writer picks, when the pick was unusable)
      allowNew    whether it may add a section
      ask         stage ids to ask the reader about (best first), when the pick isn't sure
      pageWide    the reader asks to change the whole page
      needsChange the feedback asks for a change, so an empty plan means "ask", not "nothing to do"
      work        stage ids whose full JSON the writer gets"""
    if pick is None:
        return dict(ANY_SCOPE)
    ids = [st["id"] for st in page["stages"]]
    chosen, maybe, kind = pick["chosen"], pick["maybe"], pick["kind"]
    scope = {"allowed": set(), "allowNew": kind == "new", "ask": None, "pageWide": False, "needsChange": kind in ("parts", "new"), "work": []}
    if kind == "page" and not chosen and not maybe:
        return {**scope, "pageWide": True, "needsChange": True}
    if kind == "page" and not chosen:  # the whole page, or some sections? The reader picks ("The whole page" is a choice)
        return {**scope, "ask": maybe, "work": maybe[:MAX_TARGETS], "needsChange": True}
    if len(chosen) > MAX_TARGETS:  # too many to be sure of: ask about them (concrete choices), never a silent whole-page change
        chosen, maybe = [], chosen
    if chosen:
        return {**scope, "allowed": set(chosen), "work": chosen, "needsChange": True}
    if kind == "none" or (kind is None and not maybe):
        return {**scope, "needsChange": False}
    if kind == "new":
        return scope
    if req.get("round") == 2:  # the reader answered: the unsure sections are worked on, and the writer decides among them
        cands = maybe or ids
        return {**scope, "allowed": set(cands), "work": cands[:MAX_TARGETS], "needsChange": True}
    # Not sure: the writer turns each unsure section into a concrete answer to pick from (none at all: the code asks).
    return {**scope, "ask": maybe or None, "work": maybe[:MAX_TARGETS], "needsChange": True}


def clean_plan(raw: Any, page: dict, req: dict, scope: dict | None = None) -> dict:
    """The model's plan, checked against the page, the scope (decide_scope) and the reader's words:
    {changes: [target + stageLabel], keep: [{stage, label}], pageWide, question | None, wholeOnly, unmatched}.
    unmatched: round 2 found nothing usable either (the app then offers only the whole page or cancel)."""
    scope = scope or ANY_SCOPE
    raw = raw if isinstance(raw, dict) else {}
    labels = _labels(page)
    source = _norm(f"{req['text']} {req.get('answer', '')}")
    keep_ids = list(dict.fromkeys(k for k in raw.get("keep") or [] if isinstance(k, str) and k in labels))
    raw_changes = [c for c in raw.get("changes") or [] if isinstance(c, dict)]
    q = raw.get("question") if isinstance(raw.get("question"), dict) else {}
    raw_options = [o for o in q.get("options") or [] if isinstance(o, dict)]

    def in_scope(t: dict) -> bool:
        if t["part"] == "new-section":
            return bool(scope["allowNew"])
        return (scope["allowed"] is None or t["stage"] in scope["allowed"]) and t["stage"] not in keep_ids

    picked = [c for c in raw_changes if quoted(_str(c.get("quote")), source)]
    found = clean_targets({"targets": picked}, page) if picked else None
    if found and found["targets"]:
        found = clean_targets({"targets": fit_widget_limit(place_widget_changes(found["targets"], page, source), page)}, page)
    page_wide = scope["pageWide"] or (scope["allowed"] is None and (raw.get("pageWide") is True or bool(found and found["pageWide"])))
    changes = [] if page_wide or not found else [t for t in found["targets"] if in_scope(t)]

    question = None
    if not page_wide and req.get("round") != 2:
        options = []
        for o in raw_options:
            label = js_slice(" ".join(_str(o.get("label")).split()), 0, LABEL_MAX)
            if not label:
                continue
            if o.get("part") == "page":
                options.append({"label": label, "part": "page", "stage": "", "after": "", "what": ""})
                continue
            t = _one_target(o, page)
            if t:
                t = fit_widget_limit([t], page)[0]
            if t and (in_scope(t) or (scope["ask"] and t["stage"] in scope["ask"] and t["stage"] not in keep_ids)):
                options.append({"label": label, **t, "what": js_slice(t["what"], 0, WHAT_MAX)})
        text = js_slice(" ".join(_str(q.get("text")).split()), 0, 200)
        if options and text:
            if scope["ask"] and not any(o["part"] == "page" for o in options):  # asking: the whole page stays a choice
                options = options[:MAX_OPTIONS - 1] + [{"label": WHOLE_PAGE, "part": "page", "stage": "", "after": "", "what": ""}]
            question = {"text": text, "options": options[:MAX_OPTIONS]}

    # Something should change but nothing usable is left: ask, never guess.
    unmatched = not changes and not page_wide and question is None and bool(raw_changes or raw_options or scope["needsChange"])
    if unmatched and req.get("round") != 2:
        question, unmatched = which_part_question(page, req["text"], scope["ask"]), False

    if req.get("wholeOnly") and (changes or question):  # a page that can only be changed as a whole
        changes, question, page_wide, unmatched = [], None, True, False
    return {"changes": [_with_label(t, labels) for t in changes], "keep": [{"stage": k, "label": labels[k]} for k in keep_ids],
            "pageWide": page_wide, "question": question, "wholeOnly": bool(req.get("wholeOnly")), "unmatched": unmatched}


STAGE_JSON_CHARS = 14_000  # one section's JSON for the writer (a widget's html is most of it)


def _stage_json(st: dict) -> str:
    def block(b: dict) -> dict:
        if b.get("type") != "widget":
            return b
        plan = ce_plan(b)
        return {**b, **({"plan": json_stringify(plan)} if plan else {})}  # without Jev's review scores
    return js_slice(json_stringify({**st, "blocks": [block(b) for b in st.get("blocks") or []]}), 0, STAGE_JSON_CHARS)


def scope_text(scope: dict, page: dict) -> str:
    """The writer's instruction for what it may change."""
    if scope["pageWide"]:
        return 'No single section: the reader asks to change the whole page. Set "pageWide" to true and "changes" to [].'
    if scope["allowed"] is None:
        return "Any section, and a new section: pick only what the feedback points at."
    if scope["ask"]:
        names = ", ".join(f'"{sid}"' for sid in scope["work"])
        return (f"No change yet: the app couldn't tell which of these sections the reader means: {names}. Write no \"changes\". "
                "Write a \"question\" with one option per section that fits the feedback, each a concrete change to that "
                "section from its JSON below, most likely first. Their full JSON is below.")
    names = ", ".join(f'"{sid}"' for sid in scope["work"]) if scope["work"] else ""
    if names:
        return f"Only these sections: {names}" + (", or a new section" if scope["allowNew"] else "") + ". Their full JSON is below."
    if scope["allowNew"]:
        return 'Only a new section ("new-section"), placed where it fits.'
    return 'No section: write no "changes" and no "question" (the app asks the reader which part they mean, or nothing needs changing). Still fill in "keep", "preference" and "topic".'


def writer_message(page: dict, cards: list[dict], scope: dict, req: dict, prefs: list[str]) -> str:
    log_lines = "\n".join(f"- {w}" for w in req["warnings"]) or "(nothing noted)"
    saved = "\n".join(f"- {p}" for p in prefs) or "(none)"
    by_id = {st["id"]: st for st in page["stages"]}
    work = "\n\n".join(f'Section "{sid}" (full JSON):\n{_stage_json(by_id[sid])}' for sid in scope["work"] if sid in by_id)
    n = widget_count(page)
    limit = (f"This page has {n} interactive widgets, at most {MAX_MARKED} are allowed"
             + (": no widget can be added, so change existing ones or a section's own blocks." if n >= MAX_MARKED else ".")) if n else \
        "This page has no interactive widgets: its interactive parts are ordinary blocks (charts, sliders, bars)."
    answer = f"\n\n<answer>\n{req['answer']}\n</answer>" if req.get("answer") else ""
    return (f"The page's sections (summaries):\n{fp.cards_text(cards)}\n\n"
            f"What you may change: {scope_text(scope, page)}\n\n{limit}\n\n"
            + (f"{work}\n\n" if work else "")
            + f"Build log:\n{log_lines}\n\nSaved preferences:\n{saved}\n\n<feedback>\n{req['text']}\n</feedback>{answer}")


def understand(client: anthropic.Anthropic, req: dict, prefs: list[str]) -> dict:
    """{plan, preference, topic, stageIds, pick, usd}. An unusable reply is "nothing to do" (the cost is still counted)."""
    page = page_of(req["followUp"])
    cards = fp.section_cards(page, req.get("lastChange"))
    pick, pick_usd = fp.pick_sections(client, cards, req["text"], req.get("answer", ""))
    scope = decide_scope(pick, page, req)
    user = writer_message(page, cards, scope, req, prefs)
    msg = client.messages.create(
        model=HAIKU,
        max_tokens=1500,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": data.formats()["feedback"]}},
        system=cached_system(data.prompt("FEEDBACK_SYSTEM")),
        messages=[{"role": "user", "content": user}],
    )
    usd = price_of(msg.model, msg.usage) + pick_usd
    try:
        r = json_parse(text_of(msg))
    except Exception:
        r = {}
    r = r if isinstance(r, dict) else {}
    preference = _clean(_str(r.get("preference")), PREF_MAX)
    topic = r.get("topic") if r.get("topic") in TOPICS else "other"
    return {"plan": clean_plan(r, page, req, scope), "preference": preference, "topic": topic,
            "stageIds": [st["id"] for st in page["stages"]], "pick": pick, "usd": usd}


# ---------- saved preferences ----------


def save_preference(db: Database, user_id: str, text: str, topic: str, feedback_id: str | None) -> dict:
    """Store an active preference. It replaces the reader's active one on the same topic (not "other"), and past
    MAX_ACTIVE the oldest active one is retired, so the planner's note stays short."""
    now = now_ms()
    with db.session() as s:
        active = list(s.scalars(select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.status == "active")
                                .order_by(UserPreference.created_at)))
        for p in active:
            if topic != "other" and p.topic == topic:
                p.status, p.updated_at = "replaced", now
        active = [p for p in active if p.status == "active"]
        for p in active[:max(0, len(active) - MAX_ACTIVE + 1)]:
            p.status, p.updated_at = "replaced", now
        pref = UserPreference(user_id=user_id, text=text, topic=topic, status="active", feedback_id=feedback_id, created_at=now, updated_at=now)
        s.add(pref)
        s.flush()
        return pref_json(pref)


def pref_json(p: UserPreference) -> dict:
    return {"id": p.id, "text": p.text, "topic": p.topic, "status": p.status, "createdAt": p.created_at}


def active_prefs(db: Database, user_id: str) -> list[str]:
    with db.session() as s:
        return list(s.scalars(select(UserPreference.text).where(UserPreference.user_id == user_id, UserPreference.status == "active")
                              .order_by(UserPreference.created_at)))


def list_prefs(db: Database, user_id: str) -> list[dict]:
    with db.session() as s:
        rows = s.scalars(select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.status.in_(STATUSES))
                         .order_by(UserPreference.created_at.desc()))
        return [pref_json(p) for p in rows]


def set_pref_status(db: Database, user_id: str, pref_id: str, status: str) -> dict | None:
    """Turn one of the reader's preferences on or off; None when it isn't theirs (or was replaced)."""
    with db.session() as s:
        p = s.get(UserPreference, pref_id)
        if p is None or p.user_id != user_id or p.status not in STATUSES:
            return None
        if status == "active" and p.status != "active":
            n = len(list(s.scalars(select(UserPreference.id).where(UserPreference.user_id == user_id, UserPreference.status == "active"))))
            if n >= MAX_ACTIVE:
                raise ValueError(f"You can have at most {MAX_ACTIVE} preferences on. Turn one off first.")
        p.status, p.updated_at = status, now_ms()
        return pref_json(p)


def delete_pref(db: Database, user_id: str, pref_id: str) -> bool:
    with db.session() as s:
        p = s.get(UserPreference, pref_id)
        if p is None or p.user_id != user_id:
            return False
        s.delete(p)
        return True


def prefs_hint(prefs: Any) -> str:
    """The planner's note for a reader with saved preferences; "" without any (the request is then unchanged)."""
    prefs = [p for p in prefs if isinstance(p, str) and p.strip()] if isinstance(prefs, list) else []
    if not prefs:
        return ""
    lines = "\n".join(f"- {_clean(p, PREF_MAX)}" for p in prefs[:MAX_ACTIVE])
    return ("This reader's saved preferences (follow them where they fit this topic; the rules above always come first):\n"
            f"{lines}")


def prefs_enabled() -> bool:
    return os.environ.get("PREFERENCES") != "0"


# ---------- the two routes ----------


def handle(client: anthropic.Anthropic, db: Database, user_id: str, req: dict) -> dict:
    """The /api/feedback turn: understand the feedback and save it with its plan. Nothing is saved for the reader (no
    preference) until they confirm."""
    r = understand(client, req, active_prefs(db, user_id))
    plan = r["plan"]
    with db.session() as s:
        fb = Feedback(user_id=user_id, record_id=req["recordId"],
                      text=js_slice(req["text"] + (f"\n\nAnswer: {req['answer']}" if req.get("answer") else ""), 0, TEXT_MAX),
                      page_request=js_slice("; ".join(c["what"] for c in plan["changes"]), 0, TEXT_MAX), usd=r["usd"], created_at=now_ms(),
                      plan={**plan, "stageIds": r["stageIds"], "preference": r["preference"], "topic": r["topic"], "round": req["round"], "pick": r["pick"]})
        s.add(fb)
        s.flush()
        fb_id = fb.id
    pref = {"text": r["preference"], "topic": r["topic"]} if r["preference"] else None
    return {"feedbackId": fb_id, "plan": plan, "preference": pref, "usd": r["usd"]}


def confirm(db: Database, user_id: str, feedback_id: str, body: Any) -> dict | None:
    """What the reader confirmed: {feedback: FollowUp.feedback | None (nothing to change), preference | None}. None when
    the feedback isn't theirs. Raises ValueError when it was already confirmed."""
    body = body if isinstance(body, dict) else {}
    if not FEEDBACK_ID.match(feedback_id):
        return None
    with db.session() as s:
        row = s.get(Feedback, feedback_id)
        if row is None or row.user_id != user_id or not isinstance(row.plan, dict):
            return None
        if row.confirmed is not None:
            raise ValueError("This feedback was already used. Send it again to make another change.")
        plan = row.plan
        ids = [i for i in plan.get("stageIds") or [] if isinstance(i, str)]
        page_wide = body.get("pageWide") is True
        raw = []
        for t in body.get("targets") if isinstance(body.get("targets"), list) else []:
            if isinstance(t, dict):
                raw.append({**t, "what": js_slice(_str(t.get("what")), 0, WHAT_MAX)})
        found = clean_targets({"targets": raw[:20]}, {"stages": [{"id": i} for i in ids]}) if raw and ids and not page_wide else None
        keep = [k for k in body.get("keep") if isinstance(k, str) and k in ids][:20] if isinstance(body.get("keep"), list) else []
        targets = [] if not found or found["pageWide"] else [t for t in found["targets"] if t["part"] == "new-section" or t["stage"] not in keep]
        if plan.get("wholeOnly") and targets:
            targets, page_wide = [], True
        run = {"id": row.id, "targets": targets, "keep": keep, "pageWide": page_wide} if targets or page_wide else None
        row.confirmed = run or {"targets": [], "keep": keep, "pageWide": False}
        pref_text, pref_topic = _str(plan.get("preference")), _str(plan.get("topic")) or "other"
    pref = save_preference(db, user_id, pref_text, pref_topic, feedback_id) if body.get("remember") is True and pref_text else None
    if pref:
        with db.session() as s:
            row = s.get(Feedback, feedback_id)
            if row is not None:
                row.preference_id = pref["id"]
    return {"feedback": run, "preference": pref}
