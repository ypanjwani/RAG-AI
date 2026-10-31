"""Port of shared/views.ts: math shared by the renderers and the validator's sanity check."""

from __future__ import annotations

import math

from ..js import js_max, js_min, js_round
from .expr import Scope, eval_defs, eval_number, is_num

MAX_STEPS = 2000
DIVERGE_LIMIT = 1e6


def with_defs(defs: list[dict] | None, scope: Scope) -> Scope:
    return eval_defs(defs, scope) if defs else scope


def run_trajectory(b: dict, base: Scope, max_steps: int = MAX_STEPS) -> dict:
    """x_{t+1} = f(x_t) with simultaneous updates; stops early on divergence. {"frames", "divergedAt"}."""
    defs = b.get("defs")

    def at(s: Scope) -> Scope:
        return with_defs(defs, s)

    n = eval_number(b["steps"], at(base))
    steps = int(js_max(0, js_min(max_steps, js_round(n if n == n and n != 0 else 0))))
    start: Scope = {**base, "t": 0.0}
    for s in b["state"]:
        start[s["name"]] = eval_number(s["init"], at(start))
    cur = at(start)
    frames = [cur]
    diverged_at = -1
    for t in range(1, steps + 1):
        nxt: Scope = {**cur, "t": float(t)}
        for u in b["update"]:
            nxt[u["name"]] = eval_number(u["expr"], {**cur, "t": float(t - 1)})
        bad = False
        for s in b["state"]:
            v = nxt.get(s["name"])
            if not is_num(v) or not math.isfinite(v) or abs(v) > DIVERGE_LIMIT:
                bad = True
                break
        if bad:
            diverged_at = t
            break
        cur = at(nxt)
        frames.append(cur)
    return {"frames": frames, "divergedAt": diverged_at}
