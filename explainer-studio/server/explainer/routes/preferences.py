"""The signed-in reader's saved preferences (from the Feedback button, feedback.py).

  GET    /api/preferences        -> {preferences: [{id, text, topic, status, createdAt}]}  (active and off, newest first)
  PUT    /api/preferences/<id>   {status: "active" | "off"} -> the preference
  DELETE /api/preferences/<id>   -> {ok}

Preferences belong to one user; any other id is a 404.
"""

from __future__ import annotations

from flask import Blueprint, Response, g
from limits import parse as parse_limit

from .. import feedback
from ..middleware import ApiError, ctx, json_body, json_response, rate_limit, require_auth

bp = Blueprint("preferences", __name__, url_prefix="/api/preferences")

LIMIT = parse_limit("60/minute")


def _check_id(pref_id: str) -> None:
    if not (pref_id.isalnum() and len(pref_id) <= 32):
        raise ApiError(404, "Not found.")


@bp.get("")
@require_auth
def list_all() -> Response:
    rate_limit(LIMIT, "prefs", g.user.id, "Too many requests. Please wait a minute.")
    return json_response({"preferences": feedback.list_prefs(ctx().db, g.user.id)})


@bp.put("/<pref_id>")
@require_auth
def set_status(pref_id: str) -> Response:
    _check_id(pref_id)
    rate_limit(LIMIT, "prefs", g.user.id, "Too many requests. Please wait a minute.")
    body = json_body()
    status = body.get("status") if isinstance(body, dict) else None
    if status not in feedback.STATUSES:
        raise ApiError(400, 'Status must be "active" or "off".', "invalid")
    try:
        p = feedback.set_pref_status(ctx().db, g.user.id, pref_id, status)
    except ValueError as e:
        raise ApiError(409, str(e), "too_many") from None
    if p is None:
        raise ApiError(404, "Not found.")
    return json_response(p)


@bp.delete("/<pref_id>")
@require_auth
def delete_one(pref_id: str) -> Response:
    _check_id(pref_id)
    rate_limit(LIMIT, "prefs", g.user.id, "Too many requests. Please wait a minute.")
    if not feedback.delete_pref(ctx().db, g.user.id, pref_id):
        raise ApiError(404, "Not found.")
    return json_response({"ok": True})
