"""The signed-in reader's history (ExplainerRecords).

  GET    /api/explainers           -> {records: [{id, updatedAt, status}]}  (an index: the browser keeps
                                      a local copy and fetches only records that changed)
  POST   /api/explainers/batch     {ids: [...]} (at most 50) -> {records: [...]}
  GET    /api/explainers/<id>      -> the record
  PUT    /api/explainers/<id>      the whole record -> the record as stored
  DELETE /api/explainers/<id>      -> {ok}

Records belong to one user; any other id is a 404 (never a 403, which would confirm it exists).

The three reads can write one thing: a generation that died or never started is marked as failed when it is
read (history.settle; audit F17, kept on purpose).
Deleting a record that is still generating also stops its generation at the next step, so it stops spending (F18).
"""

from __future__ import annotations

from flask import Blueprint, Response, g
from limits import parse as parse_limit
from sqlalchemy import select

from .. import history
from ..db import User
from ..js import json_stringify
from ..middleware import ApiError, ctx, json_body, json_response, rate_limit, require_auth
from ..schemas import RECORD_ID, IdList, Record, validate

bp = Blueprint("history", __name__, url_prefix="/api/explainers")

RECORD_MAX_BYTES = 3 * 1024 * 1024  # a paper's text, the page, its original and the chat
USER_MAX_BYTES = 200 * 1024 * 1024
USER_MAX_RECORDS = 2000
WRITE_LIMIT = parse_limit("240/minute")  # saves follow reading progress and chat, so they are frequent
READ_LIMIT = parse_limit("240/minute")


def _check_id(record_id: str) -> None:
    if not RECORD_ID.match(record_id):
        raise ApiError(404, "Not found.")


@bp.get("")
@require_auth
def index() -> Response:
    rate_limit(READ_LIMIT, "history-read", g.user.id, "Too many requests. Please wait a minute.")
    return json_response({"records": history.list_index(ctx().db, g.user.id)})


@bp.post("/batch")
@require_auth
def batch() -> Response:
    rate_limit(READ_LIMIT, "history-read", g.user.id, "Too many requests. Please wait a minute.")
    body = validate(IdList, json_body())
    return json_response({"records": history.get_many(ctx().db, g.user.id, body.ids)})


@bp.get("/<record_id>")
@require_auth
def get_one(record_id: str) -> Response:
    _check_id(record_id)
    rate_limit(READ_LIMIT, "history-read", g.user.id, "Too many requests. Please wait a minute.")
    found = history.get_many(ctx().db, g.user.id, [record_id])
    if not found:
        raise ApiError(404, "Not found.")
    return json_response(found[0])


@bp.put("/<record_id>")
@require_auth
def put_one(record_id: str) -> Response:
    _check_id(record_id)
    rate_limit(WRITE_LIMIT, "history-write", g.user.id, "Too many saves. Please wait a minute.")
    body = json_body()
    rec = validate(Record, body)
    if rec.id != record_id:
        raise ApiError(422, "The record id doesn't match the URL.", "invalid", {"id": "Doesn't match the URL."})
    # A quick check on what was sent, before any database work...
    if len(json_stringify(body).encode("utf-8")) > RECORD_MAX_BYTES:
        raise ApiError(413, "This explainer is too large to save.", "too_large")
    with ctx().db.session() as s:
        # One save per user at a time (a row lock on Postgres), so parallel saves can't each pass the caps (audit F16).
        s.execute(select(User.id).where(User.id == g.user.id).with_for_update())
        stored = history.save(s, g.user.id, body)
        # ...and the real check on what is stored: save() can merge in the server's spec and usage (audit F16).
        # Raising here rolls the save back.
        size = len(json_stringify(stored).encode("utf-8"))
        if size > RECORD_MAX_BYTES:
            raise ApiError(413, "This explainer is too large to save.", "too_large")
        count, used = history.storage_used(s, g.user.id, record_id)
        if count >= USER_MAX_RECORDS or used + size > USER_MAX_BYTES:
            raise ApiError(413, "Your history is full. Delete some old explainers to save new ones.", "storage_full")
    return json_response(stored)


@bp.delete("/<record_id>")
@require_auth
def delete_one(record_id: str) -> Response:
    _check_id(record_id)
    rate_limit(WRITE_LIMIT, "history-write", g.user.id, "Too many saves. Please wait a minute.")
    if not history.remove(ctx().db, g.user.id, record_id):
        raise ApiError(404, "Not found.")
    return json_response({"ok": True})
