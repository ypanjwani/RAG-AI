"""The Jev client (jev.py): a pinned model version and retries on rate limits, overload and network errors."""

from __future__ import annotations

import httpx
import pytest

from explainer import jev

Q = {"ok": {"type": "noul", "instructions": "Is it ok?"}}
BODY = {"model": "jev-1.13.0", "answers": {"ok": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 12, "output_tokens": 0}}


def fake_post(monkeypatch, replies: list) -> list[dict]:
    """httpx.post returns (or raises) the next of replies; records each request's JSON. Sleeps are skipped and recorded."""
    sent: list[dict] = []
    it = iter(replies)

    def post(url, headers, json, timeout):
        sent.append(json)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(jev.httpx, "post", post)
    monkeypatch.setattr(jev.time, "sleep", lambda s: sent.append({"slept": s}))
    return sent


def res(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, headers=headers or {})


def test_the_model_is_pinned(monkeypatch):
    sent = fake_post(monkeypatch, [res(200, BODY)])
    r = jev.jev_ask("k", "state", Q)
    assert sent[0]["model"] == jev.JEV_MODEL == "jev-1.13.0" and r["answers"]["ok"]["p"] == 0.9


def test_a_rate_limit_is_retried_with_backoff(monkeypatch):
    sent = fake_post(monkeypatch, [res(429), res(529, headers={"retry-after": "2"}), res(200, BODY)])
    assert jev.jev_ask("k", "state", Q)["answers"]["ok"]["p"] == 0.9
    assert [s.get("slept") for s in sent if "slept" in s] == [0.5, 2.0]  # the default, then Retry-After


def test_retry_after_is_capped(monkeypatch):
    sent = fake_post(monkeypatch, [res(429, headers={"retry-after": "60"}), res(200, BODY)])
    jev.jev_ask("k", "state", Q)
    assert {"slept": jev.RETRY_AFTER_MAX} in sent


def test_a_network_error_is_retried_then_reported(monkeypatch):
    fake_post(monkeypatch, [httpx.ConnectError("down")] * 3)
    with pytest.raises(jev.JevError, match="unreachable"):
        jev.jev_ask("k", "state", Q)


def test_too_many_rate_limits_give_a_readable_error(monkeypatch):
    sent = fake_post(monkeypatch, [res(429)] * 3)
    with pytest.raises(jev.JevError, match="rate limited"):
        jev.jev_ask("k", "state", Q)
    assert sum("model" in s for s in sent) == 3


def test_other_errors_are_not_retried(monkeypatch):
    sent = fake_post(monkeypatch, [res(401)])
    with pytest.raises(jev.JevError, match="invalid key"):
        jev.jev_ask("k", "state", Q)
    assert len(sent) == 1
