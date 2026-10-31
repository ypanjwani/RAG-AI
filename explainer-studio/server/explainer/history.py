"""The reader's history on the server, and the generations that fill it.

The browser owns an ExplainerRecord (it edits, chats, renames); the server owns a running generation.
They are kept in separate rows so neither overwrites the other:
- explainers: the record as the browser last saved it.
- generations: progress and result of the record's latest generation, written by the server as it runs.

A record whose status is "generating" is settled from its generation row: on finish (by the worker
itself, so it lands even if the tab was closed), or when it is read and the generation died with its
worker (STALE_MS without progress). While it runs, reads merge in its live steps.

Reads can write (audit F17, kept on purpose): settle() is the only place a generation that died with its
worker, or never started, is noticed, so GET /api/explainers, POST /batch and GET /<id> may commit that
one status change. Two reads racing on it store the same status and error (only the timestamp can differ),
so the race is harmless; the reads stay under the read rate limit.

Saving: a browser may not move a record into "generating" by itself (that is /api/generate's job), so
a stale tab can't undo a finished generation. Nor may it move a record out of "generating" while that
generation still runs (audit F13), so a stale tab can't hide the result that is on its way.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .db import Database, Explainer, Generation, User, now_ms
from .middleware import ApiError

log = logging.getLogger("explainer")

STALE_MS = 10 * 60 * 1000  # a running generation with no progress for this long died with its worker
ORPHAN_MS = 2 * 60 * 1000  # "generating" but no generation was ever started (the tab closed in between)
GEN_KEYS = ("status", "steps", "spec", "usage", "error")
INTERRUPTED = "Generation was interrupted (the server restarted). Try again."


def _apply_generation(data: dict, gen: Generation) -> dict:
    out = {**data, "steps": gen.steps or {}, "updatedAt": max(int(data.get("updatedAt") or 0), gen.updated_at)}
    if gen.status == "done" and gen.spec:
        out.update(status="done", spec=gen.spec, usage=gen.usage, title=str(gen.spec.get("title") or out.get("title", ""))[:200])
        out.pop("error", None)
    elif gen.status == "error":
        out.update(status="error", error=gen.error or "Generation failed.")
    return out


def _store(row: Explainer, data: dict) -> None:
    from .js import json_stringify
    row.data = data
    row.status = data["status"]
    row.title = str(data.get("title") or "")[:200]
    row.updated_at = int(data.get("updatedAt") or now_ms())
    row.saved_at = now_ms()
    row.size = len(json_stringify(data).encode("utf-8"))


def settle(s: Session, row: Explainer) -> dict:
    """The record as the reader should see it now (persisting a finished or dead generation)."""
    if row.status != "generating":
        return row.data
    gen = s.get(Generation, (row.user_id, row.id))
    now = now_ms()
    if gen is None:
        if now - row.saved_at > ORPHAN_MS:
            _store(row, {**row.data, "status": "error", "error": "Generation didn't start (the page was closed). Try again.", "updatedAt": now})
        return row.data
    if gen.status == "running" and now - gen.updated_at > STALE_MS:
        gen.status, gen.error, gen.updated_at = "error", INTERRUPTED, now
    if gen.status == "running":
        return {**row.data, "steps": gen.steps or {}}  # live progress, not stored
    _store(row, _apply_generation(row.data, gen))
    return row.data


def list_index(db: Database, user_id: str) -> list[dict]:
    """[{id, updatedAt, status}] for every record, newest first: the browser fetches only what changed."""
    with db.session() as s:
        # Only the three columns (audit F15): a record's JSON can be megabytes. Just the "generating" ones are
        # loaded in full, to settle them.
        rows = s.execute(select(Explainer.id, Explainer.updated_at, Explainer.status).where(Explainer.user_id == user_id)
                         .order_by(Explainer.updated_at.desc())).all()
        out = []
        for rid, updated_at, status in rows:
            if status == "generating":
                r = s.get(Explainer, (user_id, rid))
                if r is not None:
                    d = settle(s, r)
                    updated_at, status = int(d.get("updatedAt") or r.updated_at), d.get("status", r.status)
            out.append({"id": rid, "updatedAt": int(updated_at), "status": status})
        return out


def get_many(db: Database, user_id: str, ids: list[str]) -> list[dict]:
    with db.session() as s:
        rows = s.scalars(select(Explainer).where(Explainer.user_id == user_id, Explainer.id.in_(ids))).all()
        return [settle(s, r) for r in rows]


def storage_used(s: Session, user_id: str, except_id: str) -> tuple[int, int]:
    from sqlalchemy import func
    count, size = s.execute(select(func.count(), func.coalesce(func.sum(Explainer.size), 0)).where(
        Explainer.user_id == user_id, Explainer.id != except_id)).one()
    return int(count), int(size)


def save(s: Session, user_id: str, data: dict) -> dict:
    """Upsert a record from the browser. Returns what is stored."""
    row = s.get(Explainer, (user_id, data["id"]))
    if row is None:
        row = Explainer(user_id=user_id, id=data["id"], created_at=int(data["createdAt"]), thread=data.get("thread"), status=data["status"], title="", data={}, updated_at=0)
        s.add(row)
    elif data["status"] == "generating" and row.status != "generating":
        # Only /api/generate starts a generation; keep what the server knows (a stale tab, or a retry about to start).
        data = {**data, **{k: row.data[k] for k in GEN_KEYS if k in row.data}}
        for k in GEN_KEYS:
            if k not in row.data:
                data.pop(k, None)
    elif row.status == "generating" and data["status"] != "generating":
        # The other direction (audit F13): a stale tab's older "done" copy must not move a record out of a
        # generation that is still running, or the result would never be applied. Keep the server's
        # status/steps/spec while it runs; the browser's other fields (chat, reading progress) still save.
        gen = s.get(Generation, (user_id, row.id))
        if gen is not None and gen.status == "running" and now_ms() - gen.updated_at <= STALE_MS:
            data = {**{k: v for k, v in data.items() if k not in GEN_KEYS}, **{k: row.data[k] for k in GEN_KEYS if k in row.data}}
        elif gen is not None and gen.status in ("done", "error"):
            data = _apply_generation(data, gen)  # finished but not yet applied: its result wins over the tab's copy
    row.thread = data.get("thread")
    _store(row, data)
    return row.data


def remove(db: Database, user_id: str, record_id: str) -> bool:
    with db.session() as s:
        n = s.execute(delete(Explainer).where(Explainer.user_id == user_id, Explainer.id == record_id)).rowcount
        s.execute(delete(Generation).where(Generation.user_id == user_id, Generation.record_id == record_id))
    return bool(n)


# ---------- the server's side of a generation ----------


def start_generation(db: Database, user_id: str, record_id: str) -> None:
    """A fresh generation row, and the record (if saved) moved to "generating". A 409 while the record's last
    generation is still running (audit F14): two runs would write into one row and both be billed."""
    now = now_ms()
    with db.session() as s:
        # One start per user at a time (the same row lock as quota.start_generation), so two can't both pass the check.
        s.execute(update(User).where(User.id == user_id).values(token_version=User.token_version))
        gen = s.get(Generation, (user_id, record_id))
        if gen is not None and gen.status == "running" and now - gen.updated_at <= STALE_MS:
            raise ApiError(409, "This explainer is already being made. Wait for it to finish.", "already_running")
        s.execute(delete(Generation).where(Generation.user_id == user_id, Generation.record_id == record_id))
        s.add(Generation(user_id=user_id, record_id=record_id, status="running", steps={}, started_at=now, updated_at=now))
        row = s.get(Explainer, (user_id, record_id))
        if row is not None:
            d = {k: v for k, v in row.data.items() if k != "error"}
            _store(row, {**d, "status": "generating", "steps": {}, "updatedAt": max(now, int(d.get("updatedAt") or 0))})


def generation_event(db: Database, user_id: str, record_id: str, e: dict[str, Any]) -> bool:
    """Record one GenerateEvent; a final one also settles the record. Never raises (the stream must go on).
    Returns False only when the generation row is gone (the record or the account was deleted while it ran),
    so the caller can stop spending on it (audit F18)."""
    try:
        with db.session() as s:
            gen = s.get(Generation, (user_id, record_id))
            if gen is None:  # deleted while running
                return False
            now = now_ms()
            if e.get("type") == "step":
                step = {"status": e.get("status")}
                if e.get("detail"):
                    step["detail"] = e["detail"]
                gen.steps = {**(gen.steps or {}), e.get("step"): step}
            elif e.get("type") == "done":
                gen.status, gen.spec, gen.usage = "done", e.get("spec"), e.get("usage")
            elif e.get("type") == "error":
                gen.status, gen.error = "error", str(e.get("message") or "Generation failed.")[:1000]
            else:
                return True
            gen.updated_at = now
            if gen.status != "running":
                row = s.get(Explainer, (user_id, record_id))
                if row is not None and row.status == "generating":
                    _store(row, _apply_generation(row.data, gen))
    except Exception:
        log.exception("couldn't save generation progress for %s", record_id)
    return True
