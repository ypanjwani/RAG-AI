"""Accounts: register, sign in, refresh, sign out, change password, delete account.

  POST   /api/auth/register    {email, password}                -> session
  POST   /api/auth/login       {email, password}                -> session
  POST   /api/auth/refresh     (refresh cookie)                 -> session (the cookie is rotated)
  POST   /api/auth/logout      (refresh cookie)                 -> {ok}
  POST   /api/auth/logout-all  (Bearer)                         -> {ok}: every device signs out
  GET    /api/auth/me          (Bearer)                         -> {user, allowance}
  POST   /api/auth/password    (Bearer) {currentPassword, newPassword} -> session (other devices sign out)
  DELETE /api/auth/me          (Bearer) {password}              -> {ok}: the account and all its data

A session is {accessToken, expiresAt, user, allowance}; the refresh token travels only in an httpOnly,
SameSite=Strict cookie scoped to /api/auth, so page scripts never see it.

Known trade-off (audit F34): register answers 409 "email_taken" for an address that has an account, so
someone can check whether an email is registered (at most REGISTER_LIMIT tries per IP). Answering
generically would need email verification to tell the real owner, which this app doesn't do by design
(CLAUDE.md). login, unlike register, gives one answer for unknown emails and wrong passwords.
"""

from __future__ import annotations

import logging
import time
from typing import NoReturn

from flask import Blueprint, Response, g, request
from limits import parse as parse_limit
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .. import quota
from ..db import RefreshToken, User, new_id, now_ms
from ..middleware import ApiError, client_ip, client_network, ctx, error_response, json_body, json_response, rate_limit, require_auth
from ..schemas import ChangePassword, DeleteAccount, Empty, Login, Register, validate
from ..security import create_access_token, hash_password, hash_token, new_refresh_token, verify_password

log = logging.getLogger("explainer")
bp = Blueprint("auth", __name__, url_prefix="/api/auth")

COOKIE = "es_refresh"
COOKIE_PATH = "/api/auth"
REUSE_GRACE_MS = 20_000  # two tabs refreshing with the same cookie at once is not theft

REGISTER_LIMIT = parse_limit("5/hour")  # per IP
LOGIN_LIMIT = parse_limit("20/minute")  # per IP
# Failed sign-ins. The block is per email *and* network, so a stranger can't lock the owner out (audit F05);
# the looser per-email ceiling still stops guessing spread over many networks.
LOGIN_FAIL_LIMIT = parse_limit("10/15minutes")  # per (email, network)
LOGIN_FAIL_EMAIL_LIMIT = parse_limit("100/hour")  # per email, any network
REFRESH_LIMIT = parse_limit("60/minute")  # per IP
ACCOUNT_LIMIT = parse_limit("10/hour")  # password changes and deletions, per user


def public_user(u: User) -> dict:
    return {"id": u.id, "email": u.email, "plan": u.plan, "createdAt": u.created_at}


def _add_refresh(s, user_id: str, family: str | None = None, token_id: str | None = None) -> str:
    """Add a new refresh token row in session s; returns the raw token for the cookie."""
    c = ctx()
    raw = new_refresh_token()
    now = now_ms()
    s.add(RefreshToken(id=token_id or new_id(), user_id=user_id, token_hash=hash_token(raw), family_id=family or new_id(), created_at=now,
                       expires_at=now + c.settings.refresh_days * 86_400_000,
                       user_agent=request.headers.get("User-Agent", "")[:200], ip=client_ip()[:64]))
    return raw


def _issue(user: User, family: str | None = None) -> Response:
    """A session response: a fresh access token, and a new refresh token in the cookie."""
    with ctx().db.session() as s:
        raw = _add_refresh(s, user.id, family)
    return _session_response(user, raw)


def _session_response(user: User, raw: str) -> Response:
    """The session JSON for user, with raw (an already stored refresh token) in the cookie."""
    c = ctx()
    token, exp = create_access_token(user.id, user.token_version, c.settings.jwt_secret, c.settings.access_minutes)
    res = json_response({"accessToken": token, "expiresAt": exp, "user": public_user(user),
                         "allowance": quota.allowance(c.settings, c.db, user.id)})
    res.set_cookie(COOKIE, raw, max_age=c.settings.refresh_days * 86400, path=COOKIE_PATH, httponly=True,
                   secure=c.settings.production or request.is_secure, samesite="Strict")
    return res


def _clear_cookie(res: Response) -> Response:
    res.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, secure=ctx().settings.production or request.is_secure, samesite="Strict")
    return res


def _no_session(message: str = "Please sign in.") -> NoReturn:
    raise _SessionEnded(message)


class _SessionEnded(ApiError):
    def __init__(self, message: str):
        super().__init__(401, message, "no_session")


@bp.errorhandler(_SessionEnded)
def _ended(e: _SessionEnded) -> Response:
    return _clear_cookie(error_response(e.status, e.message, e.code))


@bp.post("/register")
def register() -> Response:
    rate_limit(REGISTER_LIMIT, "register", client_ip(), "Too many new accounts from this network. Please try again later.")
    c = ctx()
    net = client_network()
    # Audit F04: free accounts per network per day. Checked here but counted only once an account is made, so
    # typos (422) and "already registered" (409) don't use up a shared network's sign-ups (audit N02).
    net_limit = parse_limit(f"{c.settings.network_daily_signups}/day") if c.settings.network_daily_signups > 0 else None
    if net_limit is not None and not c.limiter.test(net_limit, "register-net", net):
        reset = c.limiter.get_window_stats(net_limit, "register-net", net).reset_time
        raise ApiError(429, "Too many new accounts from this network today. Please try again tomorrow.", "rate_limited",
                       headers={"Retry-After": str(max(1, int(reset - time.time())))})
    body = validate(Register, json_body())
    hashed = hash_password(body.password)  # slow on purpose; outside any database transaction
    try:
        with c.db.session() as s:
            if s.scalar(select(User.id).where(User.email == body.email)):
                raise ApiError(409, "An account with this email already exists. Sign in instead.", "email_taken", {"email": "Already registered."})
            user = User(email=body.email, password_hash=hashed, last_login_at=now_ms(), signup_net=net)
            s.add(user)
    except IntegrityError:  # the same email registered by a parallel request
        raise ApiError(409, "An account with this email already exists. Sign in instead.", "email_taken", {"email": "Already registered."}) from None
    if net_limit is not None:
        c.limiter.hit(net_limit, "register-net", net)
    log.info("new account %s", user.id)
    res = _issue(user)
    res.status_code = 201
    return res


@bp.post("/login")
def login() -> Response:
    rate_limit(LOGIN_LIMIT, "login", client_ip(), "Too many sign-in attempts. Please wait a minute.")
    body = validate(Login, json_body())
    c = ctx()
    net = client_network()
    if not c.limiter.test(LOGIN_FAIL_LIMIT, "login-fail", body.email, net):
        raise ApiError(429, "Too many failed sign-ins for this account. Try again in 15 minutes.", "rate_limited", headers={"Retry-After": "900"})
    if not c.limiter.test(LOGIN_FAIL_EMAIL_LIMIT, "login-fail-email", body.email):
        raise ApiError(429, "Too many failed sign-ins for this account. Try again in an hour.", "rate_limited", headers={"Retry-After": "3600"})
    with c.db.session() as s:
        user = s.scalar(select(User).where(User.email == body.email))
    # Same work and the same answer whether the email is unknown or the password wrong.
    if not verify_password(body.password, user.password_hash if user else None) or user is None or not user.is_active:
        c.limiter.hit(LOGIN_FAIL_LIMIT, "login-fail", body.email, net)
        c.limiter.hit(LOGIN_FAIL_EMAIL_LIMIT, "login-fail-email", body.email)
        raise ApiError(401, "Wrong email or password.", "bad_credentials")
    c.limiter.clear(LOGIN_FAIL_LIMIT, "login-fail", body.email, net)  # the owner's typos don't pile up
    with c.db.session() as s:
        s.execute(update(User).where(User.id == user.id).values(last_login_at=now_ms()))
        _backfill_net(s, user)
        s.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.expires_at < now_ms()))
    return _issue(user)


@bp.post("/refresh")
def refresh() -> Response:
    rate_limit(REFRESH_LIMIT, "refresh", client_ip(), "Too many requests. Please wait a minute.")
    validate(Empty, json_body() if request.get_data() else {})  # no body = {}: the answer is about the session (F31)
    raw = request.cookies.get(COOKIE, "")
    if not raw:
        _no_session()
    c = ctx()
    now = now_ms()
    with c.db.session() as s:
        row = s.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if row is None:
            _no_session()
        if row.revoked_at is not None:
            if row.replaced_by and now - row.revoked_at > REUSE_GRACE_MS:
                # A rotated token came back: someone else has a copy. End every session in this chain.
                s.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now))
                log.warning("refresh token reuse for user %s; family revoked", row.user_id)
                s.commit()
                _no_session("Your session was ended for security. Please sign in again.")
            if not row.replaced_by:
                _no_session()
            # Within the grace window, only while the token that replaced it is still live (audit N01): signing
            # out, "sign out everywhere" and a password change revoke it, so an old copy can't bring the session back.
            succ = s.get(RefreshToken, row.replaced_by)
            if succ is None or succ.revoked_at is not None or succ.expires_at <= now:
                _no_session()
        if row.expires_at <= now:
            _no_session("Your session has expired. Please sign in again.")
        user = s.get(User, row.user_id)
        if user is None or not user.is_active:
            _no_session()
        _backfill_net(s, user)
        successor = new_id()
        if row.revoked_at is None:
            # Compare-and-set (audit F20): only one of two refreshes racing with the same cookie rotates it, and
            # replaced_by names the token that replaced it, so the chain can be followed.
            rotated = s.execute(update(RefreshToken).where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
                                .values(revoked_at=now, replaced_by=successor)).rowcount == 1
            if not rotated:
                s.refresh(row)
                if not row.replaced_by:  # signed out at that moment, not rotated
                    _no_session()
        if row.replaced_by != successor:
            _grace_reuse(s, row)
        new_raw = _add_refresh(s, user.id, row.family_id, successor)
    return _session_response(user, new_raw)


def _backfill_net(s, user: User) -> None:
    """Accounts made before the per-network cap (migration 0003) have no signup_net, so they skipped that cap
    (audit F04). They get the network they sign in or refresh from, once."""
    if user.signup_net is None:
        user.signup_net = client_network()
        s.execute(update(User).where(User.id == user.id, User.signup_net.is_(None)).values(signup_net=user.signup_net))


def _grace_reuse(s, row: RefreshToken) -> None:
    """A rotated token used again within REUSE_GRACE_MS (two tabs refreshing together) is accepted once more,
    not again and again: a copied cookie can't mint any number of sessions (audit F21). The extra request
    gets a 401 but keeps its cookie, which the tab that won already replaced."""
    issued = s.scalar(select(func.count()).select_from(RefreshToken).where(
        RefreshToken.family_id == row.family_id, RefreshToken.id != row.id, RefreshToken.created_at >= row.revoked_at)) or 0
    if issued >= 2:  # the successor, plus the one grace reuse
        log.warning("refresh grace window reused again for user %s; refused", row.user_id)
        raise ApiError(401, "Please sign in.", "no_session")


@bp.post("/logout")
def logout() -> Response:
    rate_limit(REFRESH_LIMIT, "logout", client_ip(), "Too many requests. Please wait a minute.")
    raw = request.cookies.get(COOKIE, "")
    if raw:
        with ctx().db.session() as s:
            s.execute(update(RefreshToken).where(RefreshToken.token_hash == hash_token(raw), RefreshToken.revoked_at.is_(None)).values(revoked_at=now_ms()))
    return _clear_cookie(json_response({"ok": True}))


def _end_all_sessions(user_id: str) -> None:
    with ctx().db.session() as s:
        s.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))
        s.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now_ms()))


@bp.post("/logout-all")
@require_auth
def logout_all() -> Response:
    _end_all_sessions(g.user.id)
    return _clear_cookie(json_response({"ok": True}))


@bp.get("/me")
@require_auth
def me() -> Response:
    c = ctx()
    return json_response({"user": public_user(g.user), "allowance": quota.allowance(c.settings, c.db, g.user.id)})


@bp.post("/password")
@require_auth
def change_password() -> Response:
    rate_limit(ACCOUNT_LIMIT, "account", g.user.id, "Too many account changes. Please try again later.")
    body = validate(ChangePassword, json_body())
    if not verify_password(body.currentPassword, g.user.password_hash):
        raise ApiError(403, "Your current password is wrong.", "bad_credentials", {"currentPassword": "Wrong password."})
    if body.currentPassword == body.newPassword:
        raise ApiError(422, "The new password must be different.", "invalid", {"newPassword": "Same as the current one."})
    hashed = hash_password(body.newPassword)
    # One transaction (audit F19): the new password, signing out every other device (and anyone with the old
    # password) and this device's new session land together, or not at all.
    with ctx().db.session() as s:
        user = s.get(User, g.user.id)
        if user is None:  # the account was deleted from another device meanwhile
            _no_session()
        user.password_hash = hashed
        user.token_version = user.token_version + 1
        s.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now_ms()))
        s.flush()
        raw = _add_refresh(s, user.id)
    return _session_response(user, raw)  # this device stays signed in


@bp.delete("/me")
@require_auth
def delete_account() -> Response:
    rate_limit(ACCOUNT_LIMIT, "account", g.user.id, "Too many account changes. Please try again later.")
    body = validate(DeleteAccount, json_body())
    if not verify_password(body.password, g.user.password_hash):
        raise ApiError(403, "Wrong password.", "bad_credentials", {"password": "Wrong password."})
    with ctx().db.session() as s:
        user = s.get(User, g.user.id)
        if user:
            # History and sessions go with it (ON DELETE CASCADE). Usage rows stay, with user_id
            # set to NULL, so the spend still counts toward the global daily cap (audit F01).
            s.delete(user)
    log.info("deleted account %s", g.user.id)
    return _clear_cookie(json_response({"ok": True}))
