"""Explainer Studio's Flask backend: the only place the API keys exist.

Routes (each module's docstring has the details):
  /api/auth/*        accounts: register, login, refresh, logout, me, password, delete   (routes/auth.py)
  /api/explainers/*  the signed-in reader's saved history                                 (routes/history.py)
  /api/generate, /api/chat, /api/edit, /api/route, /api/section, /api/formula  the AI calls, signed in (routes/ai.py)

Every request goes through middleware.py (request id, Origin/CORS, JSON, security headers, JSON errors).
In production the same app serves the built frontend (../dist), so the API is same-origin.

Settings (environment, or explainer-studio/.env in development; the account and plan ones are in config.py):
  ANTHROPIC_API_KEY      required
  JEV_API_KEY            optional: page brief, PDF section filter, clarity check and fact check are skipped without it
  ALLOWED_ORIGINS        comma-separated origins allowed to call /api (default: the Vite dev origins; the app's own origin is always allowed)
  PLAN_EFFORT            default planning effort for requests that send none: low | medium | high (default medium)
  FACTCHECK_SECTIONS     1 = the fact check also asks whether each statement agrees with its own section (evals; default off)
  CHART_BLOCK            0 = turn the chart block off: the Quality+ planner gets the old OUTLINE_SYSTEM and experiments stay (default on; chartpick.py)
  RATELIMIT_STORAGE_URI  e.g. redis://... (default memory://, fine for one process)
  TRUST_PROXY            1 behind a proxy that sets X-Forwarded-For (Render does), so limits key on the real client IP
  TRUST_PROXY_HOPS       how many proxies in front of the app set X-Forwarded-For (default 1: Render alone; 2 with a CDN
                         such as Cloudflare in front of Render). Too low and every visitor shares one rate-limit bucket (audit F32)
  ALLOW_HIDDEN_MODES     1 = /api/generate also runs the hidden/retired modes (smart, scene-plus, code-lab, code-lab-2,
                         code-lab-3) for test scripts; otherwise they run as Quality+ (audit F09). Never set it in production
  ALLOW_DEBUG            1 = honour debugVersions / debugRejected (scripts/variety.mjs); otherwise ignored (audit F10)
  EXPLAINER_NO_DOTENV    1 = don't read explainer-studio/.env (the tests set it, so they never pick up real API keys; audit F29)
  EXPLAINER_DIST         where the built frontend is (default ../dist; the Docker image sets it)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from flask import Flask, Response, request, send_from_directory
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from werkzeug.exceptions import NotFound
from werkzeug.middleware.proxy_fix import ProxyFix

from . import middleware
from .config import Settings
from .db import Database
from .middleware import ApiError, Ctx
from .paper_cache import PaperStore
from .routes import ai, auth, history, preferences

ROOT = Path(__file__).resolve().parents[2]  # explainer-studio/ (when run from the source tree)
DIST = Path(os.environ.get("EXPLAINER_DIST") or ROOT / "dist")  # the built frontend

log = logging.getLogger("explainer")

# Used when ALLOWED_ORIGINS isn't set: the Vite dev server and `vite preview`.
DEV_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"


def _proxy_hops() -> int:
    """TRUST_PROXY_HOPS as a small positive int (default 1)."""
    try:
        hops = int(os.environ.get("TRUST_PROXY_HOPS", "1"))
    except ValueError:
        raise RuntimeError("TRUST_PROXY_HOPS must be a whole number (1 for Render alone, 2 with a CDN in front).") from None
    if not 1 <= hops <= 5:
        raise RuntimeError("TRUST_PROXY_HOPS must be between 1 and 5.")
    return hops


def create_app() -> Flask:
    if os.environ.get("EXPLAINER_NO_DOTENV") != "1":
        load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s")
    app = Flask(__name__, static_folder=None)
    if os.environ.get("TRUST_PROXY") == "1":
        hops = _proxy_hops()
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)  # type: ignore[method-assign]

    settings = Settings.from_env()
    db = Database(settings.database_url)
    if settings.database_url in ("sqlite://", "sqlite:///:memory:"):
        db.create_all()  # tests; real databases are migrated by Alembic (server/migrations)
    allowed = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", DEV_ORIGINS).split(",") if o.strip()}
    limiter = FixedWindowRateLimiter(storage_from_string(os.environ.get("RATELIMIT_STORAGE_URI", "memory://")))
    app.extensions["explainer"] = Ctx(settings=settings, db=db, limiter=limiter, allowed_origins=allowed)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    app.extensions["explainer_ai"] = {
        "client": anthropic.Anthropic(api_key=api_key) if api_key else None,
        "jev_key": os.environ.get("JEV_API_KEY") or None,
        "plan_effort": os.environ.get("PLAN_EFFORT") if os.environ.get("PLAN_EFFORT") in ("low", "high") else "medium",
        "paper_cache": PaperStore(db),
        "allow_hidden_modes": os.environ.get("ALLOW_HIDDEN_MODES") == "1",
        "allow_debug": os.environ.get("ALLOW_DEBUG") == "1",
    }

    middleware.install(app)
    for bp in (auth.bp, history.bp, ai.bp, preferences.bp):
        app.register_blueprint(bp)

    @app.get("/api/health")
    def health() -> Response:
        return middleware.json_response({"ok": True})

    @app.route("/api/<path:rest>", methods=["GET", "POST", "PUT", "DELETE"])
    def api_fallback(rest: str) -> Response:
        """An /api path no route serves: 405 if it exists with another method, else 404 (JSON, not the app page)."""
        adapter = app.url_map.bind_to_environ(request.environ)
        other = []
        for m in ("GET", "POST", "PUT", "DELETE"):
            try:
                rule, _ = adapter.match(method=m, return_rule=True)
            except Exception:
                continue
            if rule.endpoint != "api_fallback":
                other.append(m)
        if other:
            raise ApiError(405, f"Use {' or '.join(other)}.", headers={"Allow": ", ".join(other)})
        raise ApiError(404, "Not found.")

    # The built frontend (npm run build), when present: one origin for the page and the API.
    @app.get("/")
    def index() -> Response:
        if not (DIST / "index.html").exists():
            return Response("Explainer Studio API. Build the app (npm run build) to serve it from here.", mimetype="text/plain")
        return send_from_directory(DIST, "index.html")

    @app.get("/<path:path>")
    def static_file(path: str) -> Response:
        if not DIST.exists():
            raise NotFound()
        return send_from_directory(DIST, path)

    return app
