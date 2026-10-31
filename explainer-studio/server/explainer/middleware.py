"""Everything every request goes through, in order:

1. Request id: X-Request-ID (kept if the client sent a sane one), echoed on the response and in the logs.
2. Origin / CORS: an /api call from a browser must come from an allowed origin (ALLOWED_ORIGINS, plus the
   app's own origin). Unsafe methods must carry an allowed Origin, which also guards the refresh cookie
   against CSRF; same-origin GETs may omit it (browsers don't send Origin on them). Preflights are answered
   here. Credentials are allowed, so the origin is always echoed exactly, never "*".
3. JSON: request bodies must be application/json; MAX_CONTENT_LENGTH caps their size (413).
4. Auth (@require_auth on a route): a Bearer JWT, checked against the user's token_version.
5. Rate limits (rate_limit()): per user for signed-in routes, per IP (and per email) for the auth routes.
6. Responses: security headers, no-store on the API, long-lived caching for hashed assets.
7. Errors: every /api error is JSON {error, code, fields?, requestId}; no stack traces leave the server.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
import uuid
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from flask import Flask, Response, current_app, g, has_request_context, request
from limits import RateLimitItem
from limits.strategies import FixedWindowRateLimiter
from werkzeug.exceptions import HTTPException

from .config import Settings
from .db import Database, User
from .js import json_parse, json_stringify
from .security import TokenError, decode_access_token

log = logging.getLogger("explainer")

MAX_BODY_BYTES = 4 * 1024 * 1024  # a paper (300k chars) plus a follow-up outline, or a saved record, fits easily
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Ctx:
    """What the routes share, stored on app.extensions["explainer"]."""
    settings: Settings
    db: Database
    limiter: FixedWindowRateLimiter
    allowed_origins: set[str]


def ctx() -> Ctx:
    return current_app.extensions["explainer"]


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None, fields: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.code, self.fields, self.headers = status, message, code, fields, headers or {}


_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
          409: "conflict", 413: "too_large", 415: "unsupported_media_type", 422: "invalid", 429: "rate_limited",
          500: "server_error", 502: "upstream_error", 503: "unavailable"}


def json_response(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(json_stringify(body), status=status, mimetype="application/json", headers=headers or {})


def error_response(status: int, message: str, code: str | None = None, fields: dict[str, str] | None = None,
                   headers: dict[str, str] | None = None) -> Response:
    body: dict[str, Any] = {"error": message, "code": code or _CODES.get(status, "error")}
    if fields:
        body["fields"] = fields
    if has_request_context() and "request_id" in g:
        body["requestId"] = g.request_id
    return json_response(body, status, headers)


def json_body() -> Any:
    """The request's JSON body (JavaScript semantics, as the rest of the server), or a 400."""
    try:
        return json_parse(request.get_data(as_text=True))
    except ValueError:
        raise ApiError(400, "Body must be JSON.", "invalid_json") from None


def client_ip() -> str:
    return request.remote_addr or "unknown"  # ProxyFix (TRUST_PROXY=1) makes this the real client behind Render


def network_of(ip: str) -> str:
    """The network an address belongs to (IPv4 /24, IPv6 /48), so one person can't spread accounts over
    the addresses of one range. Not an address (e.g. "unknown"): the text itself."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def client_network() -> str:
    return network_of(client_ip())


def rate_limit(limit: RateLimitItem, scope: str, key: str, message: str) -> None:
    """Count one hit; past the limit, a 429 with Retry-After."""
    limiter = ctx().limiter
    if not limiter.hit(limit, scope, key):
        reset = limiter.get_window_stats(limit, scope, key).reset_time
        retry = max(1, int(reset - time.time()))
        raise ApiError(429, message, "rate_limited", headers={"Retry-After": str(retry)})


def require_auth(fn: Callable) -> Callable:
    """The route runs only with a valid access token; the user is g.user (detached from its session)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "Please sign in.", "auth_required", headers={"WWW-Authenticate": "Bearer"})
        c = ctx()
        try:
            claims = decode_access_token(token.strip(), c.settings.jwt_secret)
        except TokenError as e:
            raise ApiError(401, str(e), "invalid_token", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
        with c.db.session() as s:
            user = s.get(User, claims["sub"])
        # A deleted or disabled account, or a token issued before "sign out everywhere" / a password change.
        if user is None or not user.is_active or user.token_version != claims["ver"]:
            raise ApiError(401, "Your session has ended. Please sign in again.", "invalid_token",
                           headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = g.get("request_id", "-") if has_request_context() else "-"
        return True


def install(app: Flask) -> None:
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    for h in logging.getLogger().handlers:
        h.addFilter(_RequestIdFilter())

    def cors_headers(origin: str) -> dict[str, str]:
        return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
                "Access-Control-Expose-Headers": "X-Request-ID, Retry-After"}

    @app.before_request
    def before() -> Response | None:
        rid = request.headers.get("X-Request-ID", "")
        g.request_id = rid if _REQUEST_ID.match(rid) else uuid.uuid4().hex
        if not request.path.startswith("/api/"):
            return None
        origin = request.headers.get("Origin", "")
        own = request.host_url.rstrip("/")
        allowed = origin in ctx().allowed_origins or origin == own
        g.cors_origin = origin if allowed else ""
        if origin and not allowed:
            return error_response(403, "Origin not allowed.", "origin_not_allowed")
        if not origin and request.method in _UNSAFE | {"OPTIONS"}:
            return error_response(403, "Origin not allowed.", "origin_not_allowed")
        if request.method == "OPTIONS":
            return Response(status=204, headers={
                **cors_headers(origin),
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Request-ID",
                "Access-Control-Max-Age": "600",
            })
        if request.method in _UNSAFE and (request.content_length or request.method in ("POST", "PUT")):
            if request.mimetype != "application/json":
                return error_response(415, "Send JSON (Content-Type: application/json).")
        return None

    @app.after_request
    def after(res: Response) -> Response:
        res.headers["X-Request-ID"] = g.get("request_id", "")
        res.headers.setdefault("X-Content-Type-Options", "nosniff")
        res.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        res.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.is_secure:
            res.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith("/api/"):
            if g.get("cors_origin"):
                res.headers.update(cors_headers(g.cors_origin))
            res.headers.add("Vary", "Origin")
            res.headers.setdefault("Cache-Control", "no-store")
            res.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        else:
            # The app page: no framing by other sites, no plugins, no <base> hijacking. (Scripts aren't
            # restricted here: widgets already run in sandboxed iframes with their own CSP.)
            res.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")
            res.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            if request.path.startswith("/assets/"):  # Vite's file names carry a content hash
                res.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif res.mimetype == "text/html":
                res.headers["Cache-Control"] = "no-cache"
        return res

    @app.errorhandler(ApiError)
    def api_error(e: ApiError) -> Response:
        return error_response(e.status, e.message, e.code, e.fields, e.headers)

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException) -> Response:
        if not request.path.startswith("/api/"):
            return e.get_response()
        status = e.code or 500
        message = {404: "Not found.", 405: "Method not allowed.", 413: "That request is too large."}.get(status, e.description or "Request failed.")
        return error_response(status, message)

    @app.errorhandler(Exception)
    def unexpected(e: Exception) -> Response:
        log.exception("unhandled error")
        if not request.path.startswith("/api/"):
            return Response("Something went wrong.", status=500, mimetype="text/plain")
        return error_response(500, "Something went wrong on our side. Please try again.")
