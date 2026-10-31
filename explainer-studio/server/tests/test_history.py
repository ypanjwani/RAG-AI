"""Saved history, server-side generations (with the pipeline faked), daily quotas, the paper cache and migrations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import H, J, signup
from explainer import history, quota
from explainer.db import Database, Explainer, Generation, UsageEvent, now_ms
from explainer.paper_cache import PaperStore, paper_key
from explainer.routes import ai


def record(id="rec1", **extra) -> dict:
    return {"id": id, "title": "Dropout", "createdAt": 1000, "updatedAt": 2000, "status": "done", "prompt": {"text": "x"},
            "request": {"mode": "concept", "text": "How does dropout work?"}, "steps": {}, "progress": {"visited": [], "answers": {}},
            "chat": [], "spec": {"title": "Dropout", "stages": []}, **extra}


def db_of(app) -> Database:
    return app.extensions["explainer"].db


# ---------- history ----------


def test_history_crud(client):
    a = signup(client)
    assert client.get("/api/explainers", headers=a).get_json() == {"records": []}
    r = client.put("/api/explainers/rec1", json=record(futureField={"kept": True}), headers=a)
    assert r.status_code == 200 and r.get_json()["futureField"] == {"kept": True}
    client.put("/api/explainers/rec2", json=record("rec2", thread="rec1", updatedAt=3000), headers=a)
    index = client.get("/api/explainers", headers=a).get_json()["records"]
    assert [x["id"] for x in index] == ["rec2", "rec1"] and index[0] == {"id": "rec2", "updatedAt": 3000, "status": "done"}
    got = client.post("/api/explainers/batch", json={"ids": ["rec1", "rec2", "missing"]}, headers=a).get_json()["records"]
    assert sorted(x["id"] for x in got) == ["rec1", "rec2"]
    assert client.get("/api/explainers/rec2", headers=a).get_json()["thread"] == "rec1"
    assert client.delete("/api/explainers/rec2", headers=a).status_code == 200
    assert client.delete("/api/explainers/rec2", headers=a).status_code == 404
    assert client.get("/api/explainers/rec2", headers=a).status_code == 404


def test_history_is_private(client):
    a, b = signup(client), signup(client, "other@example.com")
    client.put("/api/explainers/rec1", json=record(), headers=a)
    # Another reader gets a 404 (not a 403), and the same id is a separate record for them.
    assert client.get("/api/explainers/rec1", headers=b).status_code == 404
    assert client.delete("/api/explainers/rec1", headers=b).status_code == 404
    assert client.post("/api/explainers/batch", json={"ids": ["rec1"]}, headers=b).get_json()["records"] == []
    client.put("/api/explainers/rec1", json=record(title="Theirs"), headers=b)
    assert client.get("/api/explainers/rec1", headers=a).get_json()["title"] == "Dropout"
    assert client.get("/api/explainers", headers=H).status_code == 401


@pytest.mark.parametrize("change,field", [
    ({"status": "finished"}, "status"),
    ({"title": "x" * 201}, "title"),
    ({"createdAt": -1}, "createdAt"),
    ({"thread": "../etc"}, "thread"),
    ({"chat": "not a list"}, "chat"),
])
def test_history_validation(client, change, field):
    r = client.put("/api/explainers/rec1", json=record(**change), headers=signup(client))
    assert r.status_code == 422 and field in r.get_json()["fields"]


def test_history_edge_cases(client):
    a = signup(client)
    r = client.put("/api/explainers/rec1", json=record("rec2"), headers=a)
    assert r.status_code == 422 and "id" in r.get_json()["fields"]
    assert client.get("/api/explainers/bad%20id", headers=a).status_code == 404
    big = record(spec={"title": "x", "stages": [], "blob": "x" * (3 * 1024 * 1024)})
    assert client.put("/api/explainers/rec1", json=big, headers=a).status_code == 413
    assert client.post("/api/explainers/batch", json={"ids": []}, headers=a).status_code == 422
    assert client.post("/api/explainers/batch", json={"ids": [str(i) for i in range(51)]}, headers=a).status_code == 422


def test_stale_tab_cant_undo_a_finished_generation(client):
    a = signup(client)
    client.put("/api/explainers/rec1", json=record(), headers=a)
    # A tab that still thinks the record is generating renames it: the title changes, the result stays.
    r = client.put("/api/explainers/rec1", json=record(status="generating", title="Renamed", spec=None, steps={"plan": {"status": "running"}}), headers=a)
    stored = r.get_json()
    assert stored["status"] == "done" and stored["spec"]["title"] == "Dropout" and stored["title"] == "Renamed"


def test_stale_tab_cant_hide_a_running_generation(client, app):
    """Audit F13: a stale tab saving its older "done" copy while a regeneration runs must not stop the
    new result from landing; the tab's other changes (chat) still save."""
    a = signup(client)
    user_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    db = db_of(app)
    client.put("/api/explainers/rec1", json=record(), headers=a)
    history.start_generation(db, user_id, "rec1")
    chat = [{"role": "user", "content": "hi"}]
    stored = client.put("/api/explainers/rec1", json=record(chat=chat), headers=a).get_json()  # the stale tab
    assert stored["status"] == "generating" and stored["chat"] == chat
    history.generation_event(db, user_id, "rec1", {"type": "done", "spec": {"title": "NEW RESULT", "stages": []}, "usage": {}})
    got = client.get("/api/explainers/rec1", headers=a).get_json()
    got = got.get("record", got)
    assert got["status"] == "done" and got["spec"]["title"] == "NEW RESULT" and got["chat"] == chat


def test_tab_can_leave_generating_once_the_run_ended(client, app):
    """A finished run doesn't hold the record: with no generation running, the browser's status is taken."""
    a = signup(client)
    user_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    db = db_of(app)
    client.put("/api/explainers/rec1", json=record(), headers=a)
    history.start_generation(db, user_id, "rec1")
    history.generation_event(db, user_id, "rec1", {"type": "error", "message": "boom"})
    stored = client.put("/api/explainers/rec1", json=record(status="error", error="boom", spec=None), headers=a).get_json()
    assert stored["status"] == "error"


# ---------- generations ----------


def fake_pipeline(monkeypatch, fail: bool = False):
    def generate(client, req, emit, **kw):
        emit({"type": "step", "step": "plan", "status": "running", "detail": "Jev: beginner"})
        emit({"type": "step", "step": "plan", "status": "done"})
        if fail:
            raise RuntimeError("boom")
        steps = [{"step": "plan", "model": "claude-haiku-5-5", "input": 100, "output": 50, "cacheRead": 900, "cacheWrite": 0, "usd": 0.006}]
        return {"spec": {"title": "Dropout, explained", "stages": []}, "usage": {"quality": "quality-plus", "effort": "low", "steps": steps, "totalUsd": 0.006, "seconds": 3}}
    monkeypatch.setattr(ai, "generate", generate)


def events(res) -> list[dict]:
    return [json.loads(line[6:]) for line in res.get_data(as_text=True).splitlines() if line.startswith("data: ")]


CONCEPT = {"mode": "concept", "text": "How does dropout work?", "quality": "quality-plus"}


def test_parallel_starts_respect_the_limits(tmp_path):
    """Audit F08: many starts at once for one user can't all pass the daily and concurrency counts."""
    import threading
    from types import SimpleNamespace

    from explainer.db import User

    db = Database(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")  # a real file: every thread gets its own connection
    db.create_all()
    with db.session() as s:
        s.add(User(id="u1", email="race@example.com", password_hash="x"))
    settings = SimpleNamespace(jwt_secret="s" * 40, daily_usd=100.0, network_daily_usd=0.0, global_daily_usd=100.0,
                               daily_generations=3, max_concurrent=2)
    started, refused = [], []
    gate = threading.Barrier(12)

    def go():
        gate.wait()
        try:
            started.append(quota.start_generation(settings, db, "u1"))
        except quota.ApiError as e:
            refused.append(e.code)

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == 2 and len(refused) == 10 and set(refused) == {"too_many_running"}
    for ev in started:  # both finish; then only one more fits today's 3
        quota.finish_generation(db, ev, 0.0)
    gate = threading.Barrier(12)
    started.clear(), refused.clear()
    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == 1 and set(refused) == {"quota_exceeded"}
    db.engine.dispose()


def test_friendly_error_hides_internals():
    """Audit F06: only messages written for the reader (and known AI-service errors) reach the browser."""
    import anthropic
    import httpx
    from explainer.llm import ReaderError, friendly_error

    assert friendly_error(ReaderError("Try a narrower topic.")) == "Try a narrower topic."
    for e in (KeyError("stages_internal_field"), RuntimeError("upstream timeout after tokens were billed"),
              ValueError(r"C:\srv\explainer\pipeline.py: bad schema node")):
        msg = friendly_error(e, "req123")
        assert str(e).strip("'") not in msg and "pipeline.py" not in msg
        assert msg == "Something went wrong on our side. Please try again (reference req123)."
    bad = anthropic.BadRequestError("prompt is too long: SECRET_PROMPT_FRAGMENT", response=httpx.Response(
        400, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    assert "SECRET_PROMPT_FRAGMENT" not in friendly_error(bad)


def test_internal_errors_stay_on_the_server(client, monkeypatch):
    """Audit F06, through the routes: the JSON 502 body and the SSE error event carry no exception text."""
    def boom(client_, req):
        raise RuntimeError("upstream timeout after tokens were billed")
    monkeypatch.setattr(ai, "route_follow_up", boom)
    a = signup(client)
    r = client.post("/api/route", json={"text": "x", "title": "T", "stages": ["a"]}, headers=a)
    body = r.get_json()
    assert r.status_code == 502 and "tokens were billed" not in body["error"] and body["requestId"]

    def gen(client_, req, emit, **kw):
        raise KeyError("stages_internal_field")
    monkeypatch.setattr(ai, "generate", gen)
    last = events(client.post("/api/generate", json=CONCEPT, headers=a))[-1]
    assert last["type"] == "error" and "stages_internal_field" not in last["message"] and "reference" in last["message"]


def test_generation_saves_to_history(client, app, monkeypatch):
    fake_pipeline(monkeypatch)
    a = signup(client)
    client.put("/api/explainers/rec1", json=record(status="generating", spec=None), headers=a)
    res = client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a)
    assert [e["type"] for e in events(res)] == ["step", "step", "done"]
    # Saved by the server itself: a reader who closed the tab finds the finished explainer.
    stored = client.get("/api/explainers/rec1", headers=a).get_json()
    assert stored["status"] == "done" and stored["title"] == "Dropout, explained" and stored["usage"]["totalUsd"] == 0.006
    assert stored["steps"]["plan"] == {"status": "done"}
    me = client.get("/api/auth/me", headers=a).get_json()["allowance"]
    assert me["generationsUsed"] == 1 and me["usdUsed"] == 0.006


def test_generation_error_is_saved_and_not_counted(client, monkeypatch):
    fake_pipeline(monkeypatch, fail=True)
    a = signup(client)
    client.put("/api/explainers/rec1", json=record(status="generating", spec=None), headers=a)
    assert events(client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a))[-1]["type"] == "error"
    stored = client.get("/api/explainers/rec1", headers=a).get_json()
    assert stored["status"] == "error" and stored["error"]
    assert client.get("/api/auth/me", headers=a).get_json()["allowance"]["generationsUsed"] == 0


def test_failed_generation_still_counts_its_spend(client, monkeypatch):
    # F02: a run that fails partway was recorded at $0, so it never hit the daily USD caps.
    def generate(client, req, emit, usage=None, **kw):
        usage.add("plan", "claude-haiku-5-5", {"input_tokens": 20_000, "output_tokens": 8_000})  # spent before it failed
        raise RuntimeError("bad model JSON")
    monkeypatch.setattr(ai, "generate", generate)
    a = signup(client)
    assert events(client.post("/api/generate", json=CONCEPT, headers=a))[-1]["type"] == "error"
    allowance = client.get("/api/auth/me", headers=a).get_json()["allowance"]
    assert allowance["usdUsed"] > 0 and allowance["generationsUsed"] == 0


def test_live_progress_and_dead_generations(client, app):
    a = signup(client)
    user_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    db = db_of(app)
    client.put("/api/explainers/rec1", json=record(status="generating", spec=None), headers=a)
    history.start_generation(db, user_id, "rec1")
    history.generation_event(db, user_id, "rec1", {"type": "step", "step": "plan", "status": "running"})
    # While it runs, a reload sees its progress.
    got = client.get("/api/explainers/rec1", headers=a).get_json()
    assert got["status"] == "generating" and got["steps"] == {"plan": {"status": "running"}}
    # A generation whose worker died (no progress for 10 minutes) becomes a retryable error.
    with db.session() as s:
        s.get(Generation, (user_id, "rec1")).updated_at = now_ms() - history.STALE_MS - 1
    got = client.get("/api/explainers/rec1", headers=a).get_json()
    assert got["status"] == "error" and "interrupted" in got["error"]
    # "generating" with no generation at all (the tab closed before it started) also settles.
    client.put("/api/explainers/rec2", json=record("rec2", status="generating", spec=None), headers=a)
    with db.session() as s:
        s.get(Explainer, (user_id, "rec2")).saved_at = now_ms() - history.ORPHAN_MS - 1
    assert client.get("/api/explainers", headers=a).get_json()["records"][0]["status"] in ("error", "done")
    assert client.get("/api/explainers/rec2", headers=a).get_json()["status"] == "error"


def test_retry_goes_through_generate(client, app, monkeypatch):
    fake_pipeline(monkeypatch)
    a = signup(client)
    client.put("/api/explainers/rec1", json=record(status="error", error="boom", spec=None), headers=a)
    # The browser's "generating" save keeps the error until /api/generate really starts...
    assert client.put("/api/explainers/rec1", json=record(status="generating", spec=None), headers=a).get_json()["status"] == "error"
    events(client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a))
    stored = client.get("/api/explainers/rec1", headers=a).get_json()
    assert stored["status"] == "done" and "error" not in stored


def test_bad_record_id(client, monkeypatch):
    fake_pipeline(monkeypatch)
    r = client.post("/api/generate", json={**CONCEPT, "recordId": "../x"}, headers=signup(client))
    assert r.status_code == 400


# ---------- quotas ----------


def test_daily_generation_quota(client, monkeypatch):
    fake_pipeline(monkeypatch)
    monkeypatch.setattr(ai, "GEN_LIMIT", ai.parse_limit("100/minute"))
    app_settings = client.application.extensions["explainer"]
    object.__setattr__(app_settings.settings, "daily_generations", 2)
    a = signup(client)
    codes = [client.post("/api/generate", json=CONCEPT, headers=a).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = client.post("/api/generate", json=CONCEPT, headers=a)
    assert r.get_json()["code"] == "quota_exceeded" and "00:00 UTC" in r.get_json()["error"]
    # Each reader has their own allowance.
    assert client.post("/api/generate", json=CONCEPT, headers=signup(client, "other@example.com")).status_code == 200


def test_spend_and_concurrency_caps(client, app):
    a = signup(client)
    user_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    c = app.extensions["explainer"]
    db = db_of(app)
    ids = [quota.start_generation(c.settings, db, user_id) for _ in range(c.settings.max_concurrent)]
    with pytest.raises(quota.ApiError) as e:
        quota.start_generation(c.settings, db, user_id)
    assert e.value.code == "too_many_running"
    quota.finish_generation(db, ids[0], 0.30)  # over the $0.25 daily allowance
    r = client.post("/api/route", json={"text": "make it shorter", "title": "T", "stages": ["a"]}, headers=a)
    assert r.status_code == 429 and r.get_json()["code"] == "quota_exceeded"
    # Everyone's spend together: past the global cap, new readers are turned away too (503).
    with db.session() as s:
        s.add(UsageEvent(user_id=user_id, kind="generate", usd=c.settings.global_daily_usd))
    r = client.post("/api/route", json={"text": "x", "title": "T", "stages": ["a"]}, headers=signup(client, "new@example.com"))
    assert r.status_code == 503 and r.get_json()["code"] == "capacity"


def test_deleted_account_spend_stays_in_global_cap(client, app):
    """Audit F01: deleting an account must not erase its spend from the global daily cap."""
    a = signup(client)
    user_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    c = app.extensions["explainer"]
    db = db_of(app)
    quota.record(c.settings, db, user_id, "generate", c.settings.global_daily_usd)
    before = quota._global_usd(db)
    assert client.delete("/api/auth/me", json={"password": "correct horse battery"}, headers=a).status_code == 200
    assert quota._global_usd(db) == pytest.approx(before)
    with db.session() as s:
        assert s.query(UsageEvent).filter(UsageEvent.user_id.is_(None)).count() == 1
    # A new account from another network is still turned away: the global cap was reached.
    r = client.post("/api/route", json={"text": "x", "title": "T", "stages": ["a"]},
                    headers=_signup_from(client, "new@example.com", "198.51.100.1"))
    assert r.status_code == 503 and r.get_json()["code"] == "capacity"
    # The same address re-registered hits its own daily allowance first: the deleted account's spend is still its (F25).
    r = client.post("/api/route", json={"text": "x", "title": "T", "stages": ["a"]},
                    headers=_signup_from(client, "reader@example.com", "192.0.2.1"))
    assert r.status_code == 429 and r.get_json()["code"] == "quota_exceeded"


def _signup_from(client, email: str, ip: str) -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": "correct horse battery"}, headers=H,
                    environ_base={"REMOTE_ADDR": ip})
    assert r.status_code == 201, r.get_json()
    return {**J, "Authorization": f"Bearer {r.get_json()['accessToken']}"}


def test_network_of():
    from explainer.middleware import network_of
    assert network_of("203.0.113.7") == network_of("203.0.113.250") == "203.0.113.0/24"
    assert network_of("::ffff:203.0.113.7") == "203.0.113.0/24"
    assert network_of("2001:db8:1:2::5") == "2001:db8:1::/48"
    assert network_of("unknown") == "unknown"


def test_network_spend_cap(client, app):
    """Audit F04: many free accounts from one network share one daily allowance, even after deletions."""
    c = app.extensions["explainer"]
    db = db_of(app)
    object.__setattr__(c.settings, "network_daily_usd", 0.40)
    route = {"text": "x", "title": "T", "stages": ["a"]}
    a = _signup_from(client, "a@example.com", "203.0.113.7")
    b = _signup_from(client, "b@example.com", "203.0.113.99")  # same /24
    other = _signup_from(client, "c@example.com", "198.51.100.1")
    a_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    quota.record(app.extensions["explainer"].settings, db, a_id, "chat", 0.20)
    quota.record(app.extensions["explainer"].settings, db, a_id, "chat", 0.20)  # A is at $0.40: their network's cap
    r = client.post("/api/route", json=route, headers=b)  # B spent nothing but shares the network
    assert r.status_code == 429 and "network" in r.get_json()["error"]
    assert client.post("/api/route", json=route, headers=other).status_code != 429  # another network is fine
    # Deleting A doesn't give the network its allowance back.
    assert client.delete("/api/auth/me", json={"password": "correct horse battery"}, headers=a).status_code == 200
    assert client.post("/api/route", json=route, headers=b).status_code == 429


def test_network_signup_cap(client, app):
    object.__setattr__(app.extensions["explainer"].settings, "network_daily_signups", 2)
    _signup_from(client, "a@example.com", "203.0.113.1")
    _signup_from(client, "b@example.com", "203.0.113.2")  # different IP, same /24: counts together
    r = client.post("/api/auth/register", json={"email": "c@example.com", "password": "correct horse battery"},
                    headers=H, environ_base={"REMOTE_ADDR": "203.0.113.3"})
    assert r.status_code == 429 and "today" in r.get_json()["error"]
    _signup_from(client, "d@example.com", "198.51.100.1")  # another network still can


# ---------- paper cache ----------


def test_paper_cache(app):
    store = PaperStore(db_of(app))
    sections = [{"heading": "Abstract", "text": "We propose..."}]
    key = paper_key("A paper", sections)
    assert key == paper_key("A paper", [dict(s) for s in sections]) and key != paper_key("A paper", [{"heading": "Abstract", "text": "We propose!"}])
    assert store.get(key) is None
    store.put(key, {"skipped": [], "kept": 1, "groups": 1, "summaries": ["s"]})
    assert store.get(key)["summaries"] == ["s"]
    assert store.prune() == 0


def test_pipeline_uses_paper_cache(monkeypatch):
    """A cached paper skips the Jev filter and the summaries."""
    from explainer import pipeline

    calls: list[str] = []
    monkeypatch.setattr(pipeline, "filter_sections", lambda *a, **k: calls.append("filter") or {"sections": a[2], "skipped": []})
    monkeypatch.setattr(pipeline, "summarize", lambda *a, **k: calls.append("summarize") or "Summary of the method.")

    class Stop(Exception):
        pass

    def stop_at_plan(*a, **k):
        raise Stop()

    monkeypatch.setattr(pipeline, "write_outline", stop_at_plan)

    class Memory(dict):
        def get(self, k):  # type: ignore[override]
            return super().get(k)

        def put(self, k, v):
            self[k] = v

    cache = Memory()
    req = {"mode": "paper", "title": "T", "note": "", "quality": "lite-plus", "sections": [{"heading": "Method", "text": "We do things."}]}
    for _ in range(2):
        steps: list[dict] = []
        with pytest.raises(Stop):
            pipeline.generate(None, req, steps.append, paper_cache=cache)
    assert calls == ["filter", "summarize"]  # only the first run read the paper
    assert any("from cache" in (e.get("detail") or "") for e in steps)


# ---------- migrations ----------


def test_migrations_match_models(tmp_path, monkeypatch):
    """alembic upgrade head builds exactly the tables the models describe."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from explainer.db import Base

    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    server = Path(__file__).resolve().parents[1]
    cfg = Config(str(server / "alembic.ini"))
    cfg.set_main_option("script_location", str(server / "migrations"))
    command.upgrade(cfg, "head")
    db = Database(url)
    with db.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    db.engine.dispose()


def test_review_rounds_get_what_is_left_of_the_allowance(client, app, monkeypatch):
    # F07: the review loop is told the user's remaining budget, so it can stop instead of overshooting it.
    seen = {}

    def generate_deep(client, req, emit, usd_budget=None, **kw):
        seen["budget"] = usd_budget
        raise RuntimeError("stop here")
    monkeypatch.setattr(ai, "generate_deep", generate_deep)
    a = signup(client)
    user_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    quota.record(app.extensions["explainer"].settings, db_of(app), user_id, "chat", 0.20)
    events(client.post("/api/generate", json={**CONCEPT, "quality": "quality-deep"}, headers=a))
    assert seen["budget"] == pytest.approx(app.extensions["explainer"].settings.daily_usd - 0.20)


def test_a_failed_start_does_not_use_up_a_generation(client, monkeypatch):
    # F12: if saving the start fails, the usage event must be closed, not left counting and "running".
    def broken(*a, **k):
        raise RuntimeError("database hiccup")
    monkeypatch.setattr(history, "start_generation", broken)
    a = signup(client)
    client.put("/api/explainers/rec1", json=record(status="generating", spec=None), headers=a)
    assert client.post("/api/generate", json={**CONCEPT, "recordId": "rec1"}, headers=a).status_code == 500
    assert client.get("/api/auth/me", headers=a).get_json()["allowance"]["generationsUsed"] == 0


def test_delete_and_reregister_keeps_the_daily_allowance(client, app):
    """Audit F25: deleting the account and registering the same email again doesn't reset today's allowance."""
    c = app.extensions["explainer"]
    a = signup(client, "again@example.com")
    a_id = client.get("/api/auth/me", headers=a).get_json()["user"]["id"]
    ev = quota.start_generation(c.settings, c.db, a_id)
    quota.finish_generation(c.db, ev, 0.05)
    quota.record(c.settings, c.db, a_id, "chat", 0.20)
    assert client.delete("/api/auth/me", json={"password": "correct horse battery"}, headers=a).status_code == 200
    again = client.get("/api/auth/me", headers=signup(client, "Again@Example.com")).get_json()["allowance"]
    assert again["generationsUsed"] == 1 and again["usdUsed"] == 0.25
    # Another address starts fresh, and the ledger holds no plain email.
    other = client.get("/api/auth/me", headers=signup(client, "fresh@example.com")).get_json()["allowance"]
    assert other["generationsUsed"] == 0 and other["usdUsed"] == 0
    with c.db.session() as s:
        keys = {e.email_key for e in s.query(UsageEvent).all()}
    assert keys and all(k and "@" not in k for k in keys)
