"""Regression tests for the low-severity audit findings (audit/audit-findings.xlsx, severity 2)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import H, signup
from explainer import history, quota
from explainer.db import Explainer, RefreshToken, UsageEvent, User
from explainer.routes import ai
from explainer.routes import auth as auth_routes

PW = "correct horse battery"
CONCEPT = {"mode": "concept", "text": "How does dropout work?", "quality": "quality-plus"}


def db_of(app):
    return app.extensions["explainer"].db


def events(res) -> list[dict]:
    return [json.loads(line[6:]) for line in res.get_data(as_text=True).splitlines() if line.startswith("data: ")]


def record(id="rec1", **extra) -> dict:
    return {"id": id, "title": "Dropout", "createdAt": 1000, "updatedAt": 2000, "status": "done", "prompt": {"text": "x"},
            "request": {"mode": "concept", "text": "x"}, "steps": {}, "progress": {"visited": [], "answers": {}},
            "chat": [], "spec": {"title": "Dropout", "stages": []}, **extra}


def capture_generate(monkeypatch) -> list[dict]:
    """Fake pipeline that keeps the request it was given."""
    seen: list[dict] = []

    def generate(client, req, emit, **kw):
        seen.append(req)
        return {"spec": {"title": "T", "stages": []}, "usage": {"quality": req["quality"], "effort": "low", "steps": [], "totalUsd": 0.0, "seconds": 1},
                "rejected": ["w"], "versions": ["v"]}
    monkeypatch.setattr(ai, "generate", generate)
    monkeypatch.setattr(ai, "generate_deep", generate)
    return seen


def usage_rows(app, kind: str) -> list[UsageEvent]:
    with db_of(app).session() as s:
        return list(s.scalars(select(UsageEvent).where(UsageEvent.kind == kind)).all())


# ---------- F09 / F10: hidden modes and debug flags ----------


@pytest.mark.parametrize("mode", ai.HIDDEN_MODES)
def test_hidden_modes_run_as_quality_plus(client, monkeypatch, mode):
    seen = capture_generate(monkeypatch)
    events(client.post("/api/generate", json={**CONCEPT, "quality": mode}, headers=signup(client)))
    assert seen[0]["quality"] == "quality-plus"


def test_hidden_modes_allowed_for_scripts(client, app, monkeypatch):
    app.extensions["explainer_ai"]["allow_hidden_modes"] = True
    seen = capture_generate(monkeypatch)
    events(client.post("/api/generate", json={**CONCEPT, "quality": "code-lab-2"}, headers=signup(client)))
    assert seen[0]["quality"] == "code-lab-2"


def test_debug_flags_ignored_unless_allowed(client, app, monkeypatch):
    seen = capture_generate(monkeypatch)
    a = signup(client)
    body = {**CONCEPT, "quality": "playground", "debugRejected": True}
    done = events(client.post("/api/generate", json=body, headers=a))[-1]
    assert "debugRejected" not in seen[0] and "rejected" not in done
    app.extensions["explainer_ai"]["allow_debug"] = True
    done = events(client.post("/api/generate", json=body, headers=a))[-1]
    assert seen[1]["debugRejected"] is True and done["rejected"] == ["w"]


# ---------- F16: storage caps on what is stored ----------


def test_size_cap_checks_the_stored_record(client, app, monkeypatch):
    from explainer.routes import history as history_routes
    a = signup(client)
    assert client.put("/api/explainers/rec1", json=record(spec={"title": "x", "stages": [], "blob": "x" * 50_000}), headers=a).status_code == 200
    client.put("/api/explainers/rec1", json=record(status="error", spec=None, error="e"), headers=a)
    # A small "generating" save gets the server's large spec merged back in: the cap must see that.
    with db_of(app).session() as s:
        row = s.get(Explainer, (s.scalar(select(User.id)), "rec1"))
        row.data = {**row.data, "spec": {"title": "x", "stages": [], "blob": "x" * 50_000}}
    monkeypatch.setattr(history_routes, "RECORD_MAX_BYTES", 20_000)
    small = record(status="generating", spec=None)
    assert client.put("/api/explainers/rec1", json=small, headers=a).status_code == 413


# ---------- F18: deleting a record stops its generation ----------


def test_deleting_a_record_stops_its_generation(client, app, monkeypatch):
    a = signup(client)
    client.put("/api/explainers/rec1", json=record(status="generating", spec=None), headers=a)
    reached: list[str] = []

    def generate(client_, req, emit, usage=None, **kw):
        usage.add("plan", "claude-haiku-5-5", {"input_tokens": 10_000, "output_tokens": 1_000})
        emit({"type": "step", "step": "plan", "status": "done"})
        with db_of(app).session() as s:
            uid = s.scalar(select(User.id))
        history.remove(db_of(app), uid, "rec1")  # the reader deletes it in another tab
        emit({"type": "step", "step": "assemble", "status": "running"})
        reached.append("after delete")  # must never run
        return {}
    monkeypatch.setattr(ai, "generate", generate)
    last = events(client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a))[-1]
    assert reached == [] and last["type"] == "error" and "deleted" in last["message"]
    ev = usage_rows(app, "generate-failed")
    assert len(ev) == 1 and ev[0].finished and ev[0].usd > 0  # the spend so far still counts


# ---------- F19: password change ----------


def test_password_change_after_account_deleted_is_401(client, app, monkeypatch):
    r = client.post("/api/auth/register", json={"email": "p@example.com", "password": PW}, headers=H)
    token = r.get_json()["accessToken"]
    real = auth_routes.hash_password

    def hash_then_delete(pw):  # the account is deleted from another device while the new hash is computed
        h = real(pw)
        with db_of(app).session() as s:
            s.delete(s.scalar(select(User)))
        return h
    monkeypatch.setattr(auth_routes, "hash_password", hash_then_delete)
    res = client.post("/api/auth/password", json={"currentPassword": PW, "newPassword": "a new long password"},
                      headers={**H, "Authorization": f"Bearer {token}"})
    assert res.status_code == 401


# ---------- F20 / F21: refresh rotation ----------


def refresh_cookie(res) -> str:
    c = next(c for c in res.headers.getlist("Set-Cookie") if c.startswith("es_refresh="))
    return c.split(";", 1)[0].split("=", 1)[1]


def test_replaced_by_points_to_the_successor(client, app):
    first = refresh_cookie(client.post("/api/auth/register", json={"email": "r@example.com", "password": PW}, headers=H))
    client.set_cookie("es_refresh", first, path="/api/auth")
    assert client.post("/api/auth/refresh", json={}, headers=H).status_code == 200
    with db_of(app).session() as s:
        rows = s.scalars(select(RefreshToken)).all()
        old = next(r for r in rows if r.revoked_at is not None)
        assert s.get(RefreshToken, old.replaced_by) is not None


def test_grace_window_reuse_is_accepted_once(client, app):
    first = refresh_cookie(client.post("/api/auth/register", json={"email": "g@example.com", "password": PW}, headers=H))
    codes = []
    for _ in range(4):
        client.set_cookie("es_refresh", first, path="/api/auth")
        codes.append(client.post("/api/auth/refresh", json={}, headers=H).status_code)
    assert codes == [200, 200, 401, 401]  # the rotation, one grace reuse, then refused
    with db_of(app).session() as s:
        live = s.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))).all()
    assert len(live) == 2


# ---------- F22: one timeout for a dead generation ----------


def test_one_stale_timeout():
    assert quota.STALE_MS == history.STALE_MS


# ---------- F23: failed chat-style calls still count their spend ----------


def test_failed_formula_turn_records_its_spend(client, app, monkeypatch):
    def turn(client_, req):
        e = ValueError("incomplete")
        e.spent_usd = 0.002
        raise e
    monkeypatch.setattr(ai, "formula_turn", turn)
    monkeypatch.setattr(ai, "parse_formula", lambda body: {})
    r = client.post("/api/formula", json={}, headers=signup(client))
    assert r.status_code == 502
    assert [round(e.usd, 4) for e in usage_rows(app, "formula")] == [0.002]


def test_chat_stream_failing_partway_records_its_spend(client, app):
    usage = SimpleNamespace(input_tokens=5000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)

    class Stream:
        current_message_snapshot = SimpleNamespace(model="claude-haiku-4-5", usage=usage)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def text_stream(self):
            yield "partial"
            raise RuntimeError("connection reset")

    app.extensions["explainer_ai"]["client"] = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream()))
    r = client.post("/api/chat", json={"context": "c", "messages": [{"role": "user", "content": "hi"}]}, headers=signup(client))
    assert "error" in events(r)[-1]
    rows = usage_rows(app, "chat")
    assert len(rows) == 1 and rows[0].usd > 0


# ---------- F24: spend of a call that outlives its account ----------


def test_record_for_deleted_account_keeps_the_spend(client, app):
    signup(client)
    db = db_of(app)
    with db.session() as s:
        uid = s.scalar(select(User.id))
        s.delete(s.get(User, uid))
    quota.record(app.extensions["explainer"].settings, db, uid, "chat", 0.01)  # used to raise IntegrityError
    rows = usage_rows(app, "chat")
    assert len(rows) == 1 and rows[0].user_id is None and rows[0].usd == 0.01


# ---------- F29 / F32: environment ----------


def test_tests_never_read_dotenv(app, monkeypatch):
    import explainer.app as app_module
    called = []
    monkeypatch.setattr(app_module, "load_dotenv", lambda *a, **k: called.append(a))
    app_module.create_app()
    assert called == [] and app.extensions["explainer_ai"]["jev_key"] is None


@pytest.mark.parametrize("value,hops", [(None, 1), ("2", 2)])
def test_proxy_hops(monkeypatch, value, hops):
    from explainer.app import _proxy_hops
    if value is None:
        monkeypatch.delenv("TRUST_PROXY_HOPS", raising=False)
    else:
        monkeypatch.setenv("TRUST_PROXY_HOPS", value)
    assert _proxy_hops() == hops


def test_proxy_hops_rejects_nonsense(monkeypatch):
    from explainer.app import _proxy_hops
    for bad in ("x", "0", "9"):
        monkeypatch.setenv("TRUST_PROXY_HOPS", bad)
        with pytest.raises(RuntimeError):
            _proxy_hops()
