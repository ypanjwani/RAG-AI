"""Accounts and the middleware around them: registration, sign-in, JWTs, refresh rotation, sign-out,
password change, account deletion, CORS and security headers."""

from __future__ import annotations

import time

import jwt
import pytest

from conftest import H, J, signup
from explainer.security import AUDIENCE, ISSUER, hash_password, verify_password

PW = "correct horse battery"


def register(client, email="reader@example.com", password=PW):
    return client.post("/api/auth/register", json={"email": email, "password": password}, headers=H)


def login(client, email="reader@example.com", password=PW):
    return client.post("/api/auth/login", json={"email": email, "password": password}, headers=H)


def bearer(token: str) -> dict:
    return {**J, "Authorization": f"Bearer {token}"}


def refresh_cookie(res) -> str:
    cookie = next(c for c in res.headers.getlist("Set-Cookie") if c.startswith("es_refresh="))
    return cookie.split(";", 1)[0].split("=", 1)[1]


def test_passwords():
    h = hash_password(PW)
    assert h.startswith("$2b$12$") and PW not in h
    assert verify_password(PW, h) and not verify_password("wrong", h)
    assert not verify_password(PW, None)  # unknown user: still does the work, never succeeds
    assert not verify_password("x" * 100, h)  # over bcrypt's 72 bytes


def test_register(client):
    r = register(client, "  Reader@Example.COM ")
    assert r.status_code == 201
    body = r.get_json()
    assert body["user"]["email"] == "reader@example.com" and "password" not in str(body) and "hash" not in str(body)
    assert body["allowance"]["generationsLimit"] == 10 and body["allowance"]["generationsUsed"] == 0
    cookie = next(c for c in r.headers.getlist("Set-Cookie") if c.startswith("es_refresh="))
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/api/auth" in cookie
    # The same address in another case is the same account.
    r = register(client, "READER@example.com")
    assert r.status_code == 409 and r.get_json()["code"] == "email_taken"


@pytest.mark.parametrize("email,password,field", [
    ("not-an-email", PW, "email"),
    ("a@b", PW, "email"),
    ("reader@example.com", "short", "password"),
    ("reader@example.com", "        ", "password"),
    ("reader@example.com", "é" * 40, "password"),  # 80 bytes: bcrypt would silently ignore the end
])
def test_register_validation(client, email, password, field):
    r = register(client, email, password)
    assert r.status_code == 422 and field in r.get_json()["fields"]


def test_register_rejects_bad_bodies(client):
    assert client.post("/api/auth/register", data="[]", headers=J).status_code == 400
    r = client.post("/api/auth/register", json={"email": "a@example.com", "password": PW, "admin": True}, headers=H)
    assert r.status_code == 422 and r.get_json()["fields"]["admin"] == "Unknown field."
    r = client.post("/api/auth/register", json={"email": "a@example.com"}, headers=H)
    assert r.status_code == 422 and r.get_json()["fields"]["password"] == "This field is required."
    assert client.post("/api/auth/register", data='{"email": 1}', headers={"Origin": H["Origin"], "Content-Type": "text/plain"}).status_code == 415


def test_register_limit(client):
    codes = [register(client, f"r{i}@example.com").status_code for i in range(6)]
    assert codes == [201] * 5 + [429]


def test_login(client):
    register(client)
    r = login(client, "READER@example.com")
    assert r.status_code == 200 and r.get_json()["accessToken"]
    wrong = login(client, password="wrong password")
    unknown = login(client, email="nobody@example.com")
    # The same answer for a wrong password and an unknown email.
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json()["error"] == unknown.get_json()["error"] == "Wrong email or password."


def test_login_lockout(client):
    register(client)
    codes = [login(client, password="nope nope").status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert login(client).status_code == 429  # even the right password, until the window passes
    assert login(client, "other@example.com").status_code == 401  # other accounts aren't locked


def test_login_lockout_is_per_network(client):
    # F05: strangers' wrong passwords must not lock the owner out from their own network.
    register(client)

    def attempt(ip):
        return client.post("/api/auth/login", json={"email": "reader@example.com", "password": "nope nope"},
                           headers=H, environ_base={"REMOTE_ADDR": ip}).status_code

    assert [attempt("198.51.100.7") for _ in range(11)] == [401] * 10 + [429]  # one attacker network: blocked
    assert [attempt(f"203.0.{i}.7") for i in range(10)] == [401] * 10  # the probe: 10 different networks
    assert login(client).status_code == 200  # the owner, from their own network, still gets in


def test_login_lockout_ceiling_across_networks(client):
    # Guessing spread over many networks still hits the per-email ceiling.
    register(client)
    codes = [client.post("/api/auth/login", json={"email": "reader@example.com", "password": "nope nope"},
                         headers=H, environ_base={"REMOTE_ADDR": f"10.{i // 250}.{i % 250}.1"}).status_code for i in range(101)]
    assert codes[:100] == [401] * 100 and codes[100] == 429


def test_me_and_tokens(client, app):
    a = signup(client)
    r = client.get("/api/auth/me", headers=a)
    assert r.status_code == 200 and r.get_json()["user"]["email"] == "reader@example.com"
    assert client.get("/api/auth/me", headers=H).status_code == 401
    assert client.get("/api/auth/me", headers={**H, "Authorization": "Basic abc"}).status_code == 401
    claims = jwt.decode(a["Authorization"][7:], options={"verify_signature": False})
    secret = app.extensions["explainer"].settings.jwt_secret
    now = int(time.time())

    def forged(**changes):
        return client.get("/api/auth/me", headers=bearer(jwt.encode({**claims, **changes}, secret, algorithm="HS256")))

    assert forged().status_code == 200
    expired = forged(exp=now - 120, iat=now - 1000, nbf=now - 1000)
    assert expired.status_code == 401 and "expired" in expired.get_json()["error"]
    assert forged(aud="someone-else").status_code == 401
    assert forged(iss="someone-else").status_code == 401
    assert forged(typ="refresh").status_code == 401
    assert forged(ver=claims["ver"] + 1).status_code == 401
    assert forged(sub="no-such-user").status_code == 401
    # Another key, and the "none" algorithm, are refused.
    assert client.get("/api/auth/me", headers=bearer(jwt.encode(claims, "another-secret-" + "y" * 40, algorithm="HS256"))).status_code == 401
    unsigned = jwt.encode(claims, None, algorithm="none")
    assert client.get("/api/auth/me", headers=bearer(unsigned)).status_code == 401
    assert claims["aud"] == AUDIENCE and claims["iss"] == ISSUER


def test_refresh_rotation(client):
    r = register(client)
    first = refresh_cookie(r)
    client.set_cookie("es_refresh", first, path="/api/auth")
    r = client.post("/api/auth/refresh", json={}, headers=H)
    assert r.status_code == 200 and r.get_json()["accessToken"]
    second = refresh_cookie(r)
    assert second != first
    # Within a few seconds, the old cookie still works (two tabs refreshing together)...
    client.set_cookie("es_refresh", first, path="/api/auth")
    assert client.post("/api/auth/refresh", json={}, headers=H).status_code == 200


def test_refresh_reuse_revokes_the_chain(client, app, monkeypatch):
    r = register(client)
    first = refresh_cookie(r)
    client.set_cookie("es_refresh", first, path="/api/auth")
    second = refresh_cookie(client.post("/api/auth/refresh", json={}, headers=H))
    # ...but long after rotation, the old cookie coming back means it was copied: every session in the chain ends.
    from explainer.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, "REUSE_GRACE_MS", -1)
    client.set_cookie("es_refresh", first, path="/api/auth")
    r = client.post("/api/auth/refresh", json={}, headers=H)
    assert r.status_code == 401 and "security" in r.get_json()["error"]
    client.set_cookie("es_refresh", second, path="/api/auth")
    assert client.post("/api/auth/refresh", json={}, headers=H).status_code == 401


def test_refresh_needs_cookie_and_origin(client):
    assert client.post("/api/auth/refresh", json={}, headers=H).status_code == 401
    r = register(client)
    client.set_cookie("es_refresh", refresh_cookie(r), path="/api/auth")
    # A cross-site page can't use the cookie: the Origin check refuses it first.
    assert client.post("/api/auth/refresh", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/auth/refresh", json={}).status_code == 403


def test_logout(client):
    r = register(client)
    cookie = refresh_cookie(r)
    client.set_cookie("es_refresh", cookie, path="/api/auth")
    assert client.post("/api/auth/logout", json={}, headers=H).status_code == 200
    client.set_cookie("es_refresh", cookie, path="/api/auth")
    assert client.post("/api/auth/refresh", json={}, headers=H).status_code == 401


def test_logout_everywhere(client):
    r = register(client)
    token, cookie = r.get_json()["accessToken"], refresh_cookie(r)
    other = login(client).get_json()["accessToken"]
    assert client.post("/api/auth/logout-all", json={}, headers=bearer(token)).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(other)).status_code == 401
    client.set_cookie("es_refresh", cookie, path="/api/auth")
    assert client.post("/api/auth/refresh", json={}, headers=H).status_code == 401


def test_change_password(client):
    r = register(client)
    old_token = r.get_json()["accessToken"]
    bad = client.post("/api/auth/password", json={"currentPassword": "wrong", "newPassword": "a new long password"}, headers=bearer(old_token))
    assert bad.status_code == 403
    same = client.post("/api/auth/password", json={"currentPassword": PW, "newPassword": PW}, headers=bearer(old_token))
    assert same.status_code == 422
    ok = client.post("/api/auth/password", json={"currentPassword": PW, "newPassword": "a new long password"}, headers=bearer(old_token))
    assert ok.status_code == 200
    assert client.get("/api/auth/me", headers=bearer(old_token)).status_code == 401  # older tokens end
    assert client.get("/api/auth/me", headers=bearer(ok.get_json()["accessToken"])).status_code == 200  # this device stays in
    assert login(client).status_code == 401 and login(client, password="a new long password").status_code == 200


def test_delete_account(client):
    a = signup(client)
    rec = {"id": "rec1", "title": "T", "createdAt": 1, "updatedAt": 1, "status": "done", "prompt": {"text": "x"},
           "request": {"mode": "concept", "text": "x"}, "steps": {}, "progress": {}, "chat": []}
    assert client.put("/api/explainers/rec1", json=rec, headers=a).status_code == 200
    assert client.delete("/api/auth/me", json={"password": "wrong"}, headers=a).status_code == 403
    assert client.delete("/api/auth/me", json={"password": PW}, headers=a).status_code == 200
    assert client.get("/api/auth/me", headers=a).status_code == 401
    assert login(client).status_code == 401
    # The address is free again, with an empty history.
    b = signup(client)
    assert client.get("/api/explainers", headers=b).get_json()["records"] == []


def test_headers(client):
    r = client.get("/api/health", headers=H)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Access-Control-Allow-Origin"] == H["Origin"] and "Origin" in r.headers["Vary"]
    assert len(r.headers["X-Request-ID"]) >= 8
    assert client.get("/api/health", headers={**H, "X-Request-ID": "my-request-123"}).headers["X-Request-ID"] == "my-request-123"
    # Same-origin GETs come without an Origin header; they get no CORS headers.
    r = client.get("/api/health")
    assert r.status_code == 200 and "Access-Control-Allow-Origin" not in r.headers
    # Errors are JSON with a code and the request id.
    body = client.get("/api/nope", headers=H).get_json()
    assert body["code"] == "not_found" and body["requestId"]
    r = client.post("/api/auth/login", data="x" * (5 * 1024 * 1024), headers=J)
    assert r.status_code == 413 and r.get_json()["code"] == "too_large"


def test_production_needs_secrets(monkeypatch):
    from explainer.config import Settings
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        Settings.from_env()
    monkeypatch.setenv("JWT_SECRET", "z" * 40)
    s = Settings.from_env()
    assert s.production and s.database_url == "postgresql+psycopg://u:p@host/db"
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        Settings.from_env()


def test_refresh_without_a_body_is_a_session_answer(client):
    # F31: no body means {}; the client hears about its session (401), not "Body must be JSON." (400).
    r = client.post("/api/auth/refresh", headers=J)  # JSON content type, empty body
    assert r.status_code == 401 and r.get_json()["error"] != "Body must be JSON."


def test_logout_is_rate_limited(client):
    # F33: logout writes to the database, so it is limited like refresh.
    codes = [client.post("/api/auth/logout", headers=J).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
