"""Regression tests for the v1 must-fix findings of the routes re-check (audit/audit-recheck-2026-10-29.md):
N01 (grace window after signing out), F15 (history index columns), F04/N02 (network sign-ups and caps), F14 (one run per record)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select

from conftest import H, J
from explainer import history, quota
from explainer.db import Generation, User, now_ms
from explainer.routes import ai

PW = "correct horse battery"
CONCEPT = {"mode": "concept", "text": "How does dropout work?", "quality": "quality-plus"}
ROUTE = {"text": "x", "title": "T", "stages": ["a"]}


def db_of(app):
    return app.extensions["explainer"].db


def signup_from(client, email: str, ip: str = "192.0.2.1") -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": PW}, headers=H, environ_base={"REMOTE_ADDR": ip})
    assert r.status_code == 201, r.get_json()
    return {**J, "Authorization": f"Bearer {r.get_json()['accessToken']}"}


def record(id="rec1", **extra) -> dict:
    return {"id": id, "title": "Dropout", "createdAt": 1000, "updatedAt": 2000, "status": "done", "prompt": {"text": "x"},
            "request": {"mode": "concept", "text": "x"}, "steps": {}, "progress": {"visited": [], "answers": {}},
            "chat": [], "spec": {"title": "Dropout", "stages": []}, **extra}


def cookie(client) -> str:
    return client.get_cookie("es_refresh", path="/api/auth").value


def refresh_with(client, raw: str):
    client.set_cookie("es_refresh", raw, path="/api/auth")
    return client.post("/api/auth/refresh", json={}, headers=H)


# ---------- N01: a rotated cookie can't outlive the end of its session ----------


@pytest.mark.parametrize("action", ["logout-all", "password", "logout"])
def test_rotated_cookie_cant_bring_back_an_ended_session(client, action):
    signup_from(client, "owner@example.com")
    stolen = cookie(client)
    r = refresh_with(client, stolen)  # the thief refreshes: stolen is now rotated, inside the grace window
    assert r.status_code == 200
    successor = cookie(client)
    auth = {**J, "Authorization": f"Bearer {r.get_json()['accessToken']}"}
    if action == "logout-all":
        assert client.post("/api/auth/logout-all", json={}, headers=auth).status_code == 200
    elif action == "password":
        assert client.post("/api/auth/password", json={"currentPassword": PW, "newPassword": "a brand new password"}, headers=auth).status_code == 200
    else:
        client.set_cookie("es_refresh", successor, path="/api/auth")
        assert client.post("/api/auth/logout", json={}, headers=H).status_code == 200
    assert refresh_with(client, stolen).status_code == 401


def test_two_tabs_refreshing_together_still_work(client):
    signup_from(client, "tabs@example.com")
    old = cookie(client)
    assert [refresh_with(client, old).status_code, refresh_with(client, old).status_code] == [200, 200]


# ---------- F15: the index doesn't load the records' JSON ----------


def test_index_reads_only_the_columns_it_returns(client, app):
    a = signup_from(client, "index@example.com")
    client.put("/api/explainers/rec1", json=record(), headers=a)
    client.put("/api/explainers/rec2", json=record("rec2", status="generating", spec=None), headers=a)
    uid = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    from explainer.db import Explainer
    with db_of(app).session() as s:  # rec2 was never started and is old: settled as an orphan when read
        s.get(Explainer, (uid, "rec2")).saved_at = now_ms() - history.ORPHAN_MS - 1000
    sql: list[str] = []
    engine = db_of(app).engine

    def capture(conn, cursor, statement, *args):
        if "FROM explainers" in statement and "ORDER BY" in statement:
            sql.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        records = client.get("/api/explainers", headers=a).get_json()["records"]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert sql and all("explainers.data" not in q for q in sql)
    by_id = {r["id"]: r for r in records}
    assert by_id["rec1"] == {"id": "rec1", "updatedAt": 2000, "status": "done"}
    assert by_id["rec2"]["status"] == "error"


# ---------- F04 / N02: network sign-ups and legacy accounts ----------


def test_failed_signups_dont_use_up_the_network(client, app):
    object.__setattr__(app.extensions["explainer"].settings, "network_daily_signups", 3)
    for i in range(3):
        r = client.post("/api/auth/register", json={"email": "not-an-email", "password": PW}, headers=H,
                        environ_base={"REMOTE_ADDR": f"203.0.113.{i + 1}"})
        assert r.status_code == 422
    signup_from(client, "a@example.com", "203.0.113.10")
    signup_from(client, "b@example.com", "203.0.113.11")
    r = client.post("/api/auth/register", json={"email": "a@example.com", "password": PW}, headers=H,
                    environ_base={"REMOTE_ADDR": "203.0.113.12"})
    assert r.status_code == 409  # already registered: doesn't count either
    signup_from(client, "c@example.com", "203.0.113.13")
    r = client.post("/api/auth/register", json={"email": "d@example.com", "password": PW}, headers=H,
                    environ_base={"REMOTE_ADDR": "203.0.113.14"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_legacy_account_gets_a_network_and_its_cap(client, app, monkeypatch):
    monkeypatch.setattr(ai, "route_follow_up", lambda client_, req: {"kind": "new", "usd": 0.0})
    c = app.extensions["explainer"]
    object.__setattr__(c.settings, "network_daily_usd", 0.10)
    signup_from(client, "old@example.com", "203.0.113.9")
    with c.db.session() as s:
        user = s.scalar(select(User).where(User.email == "old@example.com"))
        user.signup_net = None  # made before migration 0003
        uid = user.id
    r = client.post("/api/auth/refresh", json={}, headers=H, environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert r.status_code == 200
    with c.db.session() as s:
        assert s.get(User, uid).signup_net == "203.0.113.0/24"
    quota.record(c.settings, c.db, uid, "chat", 0.20)
    auth = {**J, "Authorization": f"Bearer {r.get_json()['accessToken']}"}
    r = client.post("/api/route", json=ROUTE, headers=auth)
    assert r.status_code == 429 and "network" in r.get_json()["error"]


def test_login_backfills_the_network(client, app):
    c = app.extensions["explainer"]
    signup_from(client, "old2@example.com")
    with c.db.session() as s:
        s.scalar(select(User).where(User.email == "old2@example.com")).signup_net = None
    r = client.post("/api/auth/login", json={"email": "old2@example.com", "password": PW}, headers=H,
                    environ_base={"REMOTE_ADDR": "198.51.100.20"})
    assert r.status_code == 200
    with c.db.session() as s:
        assert s.scalar(select(User.signup_net).where(User.email == "old2@example.com")) == "198.51.100.0/24"


# ---------- F14: one generation per record at a time ----------


def _fake_generate(monkeypatch):
    def generate(client_, req, emit, **kw):
        return {"spec": {"title": "T", "stages": []}, "usage": {"quality": "q", "effort": "low", "steps": [], "totalUsd": 0.0, "seconds": 1}}
    monkeypatch.setattr(ai, "generate", generate)


def _events(res) -> list[dict]:
    return [json.loads(line[6:]) for line in res.get_data(as_text=True).splitlines() if line.startswith("data: ")]


def test_second_generation_on_a_running_record_is_refused(client, app, monkeypatch):
    _fake_generate(monkeypatch)
    a = signup_from(client, "gen@example.com")
    uid = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    client.put("/api/explainers/rec1", json=record(), headers=a)
    history.start_generation(db_of(app), uid, "rec1")  # run A is running
    r = client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a)
    assert r.status_code == 409 and r.get_json()["code"] == "already_running"
    assert client.get("/api/auth/me", headers=a).get_json()["allowance"]["generationsUsed"] == 0
    with db_of(app).session() as s:
        assert s.get(Generation, (uid, "rec1")).status == "running"  # run A's row is untouched


@pytest.mark.parametrize("state", ["error", "done", "stale"])
def test_finished_or_dead_generation_can_be_run_again(client, app, monkeypatch, state):
    _fake_generate(monkeypatch)
    a = signup_from(client, "again@example.com")
    uid = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    client.put("/api/explainers/rec1", json=record(), headers=a)
    history.start_generation(db_of(app), uid, "rec1")
    with db_of(app).session() as s:
        gen = s.get(Generation, (uid, "rec1"))
        if state == "stale":
            gen.updated_at = now_ms() - history.STALE_MS - 1000
        else:
            gen.status = state
    r = client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a)
    assert r.status_code == 200 and _events(r)[-1]["type"] == "done"
