"""Shared fixtures: an app on a fresh in-memory database, and helpers to sign up and call the API."""

from __future__ import annotations

import pytest

from explainer.app import create_app

ORIGIN = "http://localhost:5173"
H = {"Origin": ORIGIN}
J = {"Origin": ORIGIN, "Content-Type": "application/json"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("JWT_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")
    # Never read the developer's .env: a deleted variable would be loaded back from it, real Jev key included (audit F29).
    monkeypatch.setenv("EXPLAINER_NO_DOTENV", "1")
    for name in ("JEV_API_KEY", "ENV", "FREE_DAILY_GENERATIONS", "FREE_DAILY_USD", "GLOBAL_DAILY_USD", "TRUST_PROXY",
                 "TRUST_PROXY_HOPS", "ALLOW_HIDDEN_MODES", "ALLOW_DEBUG"):
        monkeypatch.delenv(name, raising=False)
    return create_app()


@pytest.fixture
def client(app):
    return app.test_client()


def signup(client, email: str = "reader@example.com", password: str = "correct horse battery") -> dict:
    """Register; returns JSON request headers carrying the new access token."""
    r = client.post("/api/auth/register", json={"email": email, "password": password}, headers=H)
    assert r.status_code == 201, r.get_json()
    return {**J, "Authorization": f"Bearer {r.get_json()['accessToken']}"}
