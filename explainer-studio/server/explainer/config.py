"""Settings, read once from the environment (or explainer-studio/.env in development) when the app starts.

The SaaS settings (the AI ones are listed in app.py's docstring):
  DATABASE_URL           postgres://... or sqlite:///... (default: sqlite file server/instance/dev.db, development only)
  JWT_SECRET             required in production (32+ chars); development makes a throwaway one per process
  ENV                    "production" makes the missing-secret and database checks fatal (the Docker image sets it)
  ACCESS_TOKEN_MINUTES   lifetime of the JWT access token (default 15)
  REFRESH_TOKEN_DAYS     lifetime of the refresh cookie (default 30)
  FREE_DAILY_GENERATIONS explainers per user per UTC day (default 10)
  FREE_DAILY_USD         AI spend per user per UTC day, generations and chat together (default 0.25)
  GLOBAL_DAILY_USD       AI spend for all users together per UTC day; a guard on the API bill (default 10)
  MAX_CONCURRENT_GENERATIONS  generations one user may run at once (default 2)
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("explainer")

SERVER_DIR = Path(__file__).resolve().parents[1]  # explainer-studio/server


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default


def database_url(raw: str | None) -> str:
    """Render and Heroku hand out postgres:// URLs; SQLAlchemy wants an explicit driver (psycopg 3)."""
    if not raw:
        (SERVER_DIR / "instance").mkdir(exist_ok=True)
        return f"sqlite:///{(SERVER_DIR / 'instance' / 'dev.db').as_posix()}"
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw


@dataclass(frozen=True)
class Settings:
    production: bool
    database_url: str
    jwt_secret: str
    access_minutes: int
    refresh_days: int
    daily_generations: int
    daily_usd: float
    global_daily_usd: float
    max_concurrent: int
    network_daily_usd: float = 2.0  # all accounts made from one network together (audit F04); 0 = off
    network_daily_signups: int = 20  # new accounts per network per day; 0 = off

    @staticmethod
    def from_env() -> "Settings":
        production = os.environ.get("ENV", "").lower() == "production"
        secret = os.environ.get("JWT_SECRET", "")
        if len(secret) < 32:
            if production:
                raise RuntimeError("JWT_SECRET must be set to at least 32 characters in production.")
            if secret:
                log.warning("JWT_SECRET is shorter than 32 characters; using a throwaway secret instead")
            # Development: sessions last until the server restarts (the Flask reloader starts a new process).
            secret = secrets.token_urlsafe(48)
        raw_db = os.environ.get("DATABASE_URL")
        if production and not raw_db:
            raise RuntimeError("DATABASE_URL must be set in production.")
        return Settings(
            production=production,
            database_url=database_url(raw_db),
            jwt_secret=secret,
            access_minutes=max(1, int(_num("ACCESS_TOKEN_MINUTES", 15))),
            refresh_days=max(1, int(_num("REFRESH_TOKEN_DAYS", 30))),
            daily_generations=max(0, int(_num("FREE_DAILY_GENERATIONS", 10))),
            daily_usd=max(0.0, _num("FREE_DAILY_USD", 0.25)),
            global_daily_usd=max(0.0, _num("GLOBAL_DAILY_USD", 10)),
            max_concurrent=max(1, int(_num("MAX_CONCURRENT_GENERATIONS", 2))),
            network_daily_usd=max(0.0, _num("NETWORK_DAILY_USD", 2)),
            network_daily_signups=max(0, int(_num("NETWORK_DAILY_SIGNUPS", 20))),
        )
