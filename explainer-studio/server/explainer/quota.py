"""The free plan's daily allowance, read from usage_events (UTC days).

Per user: FREE_DAILY_GENERATIONS explainers and FREE_DAILY_USD of AI spend (generations, chat, edit,
route, section and formula calls together), and at most MAX_CONCURRENT_GENERATIONS running at once. For everyone
together: GLOBAL_DAILY_USD, so a wave of sign-ups can't run up an unbounded bill. Per network (the
IPv4 /24 or IPv6 /48 an account was made from): NETWORK_DAILY_USD, so one person making many free accounts
can't use up the global cap and take the service down for everyone (audit F04).

"Per user" means per account *or* per email address: every usage row also carries email_key (a keyed hash of
the account's email), so deleting the account and registering the same address again the same day doesn't
reset the allowance (audit F25). The key is an HMAC with JWT_SECRET, so the ledger doesn't reveal a deleted
account's address; rotating JWT_SECRET only forgets the link for the rest of that day.

A generation is counted when it starts (an unfinished event) and priced when it ends, so starting many
at once can't slip past the count. One that fails becomes kind "generate-failed": its spend still counts, but it
is no longer one of the day's explainers. Quota errors are 429s with code "quota_exceeded".
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from sqlalchemy import func, or_, select, update

from .db import Database, UsageEvent, User, now_ms
from .config import Settings
from .history import STALE_MS as _HISTORY_STALE_MS
from .middleware import ApiError

log = logging.getLogger("explainer")

# An unfinished generation older than this died with its worker; it no longer counts as running. The same
# timeout as history.STALE_MS (measured there from the last progress, here from the start, so it is never
# later): once the record says "interrupted, try again", the retry isn't refused as "too_many_running" (audit F22).
STALE_MS = _HISTORY_STALE_MS


def day_start_ms(t: float | None = None) -> int:
    t = time.time() if t is None else t
    return int(t // 86400 * 86400 * 1000)


def reset_note() -> str:
    left = int(day_start_ms() / 1000 + 86400 - time.time())
    return f"It resets at 00:00 UTC (in {left // 3600} h {left % 3600 // 60} min)."


def email_key(settings: Settings, email: str) -> str:
    """The usage rows' link to an email address (audit F25): an HMAC, so it can't be reversed without the secret."""
    return hmac.new(settings.jwt_secret.encode(), b"usage-email:" + email.strip().lower().encode(), hashlib.sha256).hexdigest()


def _key_of(s, settings: Settings, user_id: str) -> str | None:
    email = s.scalar(select(User.email).where(User.id == user_id))
    return email_key(settings, email) if email else None


def _mine(user_id: str, key: str | None):
    """Usage rows that count toward this user's allowance: theirs, and those of a deleted account with the same email."""
    return or_(UsageEvent.user_id == user_id, UsageEvent.email_key == key) if key else UsageEvent.user_id == user_id


def usage_today(settings: Settings, db: Database, user_id: str) -> dict:
    start = day_start_ms()
    with db.session() as s:
        mine = _mine(user_id, _key_of(s, settings, user_id))
        gens = s.scalar(select(func.count()).select_from(UsageEvent).where(
            mine, UsageEvent.kind == "generate", UsageEvent.created_at >= start)) or 0
        usd = s.scalar(select(func.coalesce(func.sum(UsageEvent.usd), 0.0)).where(
            mine, UsageEvent.created_at >= start)) or 0.0
    return {"generations": int(gens), "usd": float(usd)}


def allowance(settings: Settings, db: Database, user_id: str) -> dict:
    """What /api/auth/me shows: today's use against the plan."""
    u = usage_today(settings, db, user_id)
    return {"plan": "free", "generationsUsed": u["generations"], "generationsLimit": settings.daily_generations,
            "usdUsed": round(u["usd"], 4), "usdLimit": settings.daily_usd, "resetsAt": day_start_ms() + 86_400_000}


def _global_usd(db: Database) -> float:
    with db.session() as s:
        return float(s.scalar(select(func.coalesce(func.sum(UsageEvent.usd), 0.0)).where(UsageEvent.created_at >= day_start_ms())) or 0.0)


def _net_of(s, user_id: str) -> str | None:
    return s.scalar(select(User.signup_net).where(User.id == user_id))


def _net_usd(db: Database, net: str) -> float:
    with db.session() as s:
        return float(s.scalar(select(func.coalesce(func.sum(UsageEvent.usd), 0.0)).where(
            UsageEvent.net == net, UsageEvent.created_at >= day_start_ms())) or 0.0)


_warned_day = 0  # per process: the day the 50% warning was last logged


def _warn_global(settings: Settings, spent: float) -> None:
    global _warned_day
    day = day_start_ms()
    if settings.global_daily_usd > 0 and spent >= settings.global_daily_usd / 2 and _warned_day != day:
        _warned_day = day
        log.warning("global AI spend at $%.2f of the $%.2f daily cap", spent, settings.global_daily_usd)


def check_spend(settings: Settings, db: Database, user_id: str) -> None:
    """Before any paid call: today's spend, the user's, their network's and everyone's."""
    if usage_today(settings, db, user_id)["usd"] >= settings.daily_usd:
        raise ApiError(429, f"You've used today's free AI allowance. {reset_note()}", "quota_exceeded")
    if settings.network_daily_usd > 0:
        with db.session() as s:
            net = _net_of(s, user_id)
        if net and _net_usd(db, net) >= settings.network_daily_usd:
            raise ApiError(429, f"Free accounts on your network have used today's AI allowance. {reset_note()}", "quota_exceeded")
    spent = _global_usd(db)
    _warn_global(settings, spent)
    if spent >= settings.global_daily_usd:
        raise ApiError(503, "Explainer Studio has reached today's capacity. Please try again tomorrow.", "capacity")


def remaining_usd(settings: Settings, db: Database, user_id: str) -> float:
    """What may still be spent today: the user's allowance or everyone's, whichever is smaller."""
    return max(0.0, min(settings.daily_usd - usage_today(settings, db, user_id)["usd"], settings.global_daily_usd - _global_usd(db)))


def start_generation(settings: Settings, db: Database, user_id: str) -> str:
    """Count a generation (or refuse it); returns the usage event's id, to price when it ends."""
    check_spend(settings, db, user_id)
    start = day_start_ms()
    with db.session() as s:
        # One start at a time per user (audit F08): a no-op write to the user's row first. Postgres row-locks it
        # and SQLite takes its write lock, so a parallel start waits here until this one commits, then counts it.
        # (Postgres runs READ COMMITTED, so the counts below see rows committed while we waited.)
        s.execute(update(User).where(User.id == user_id).values(token_version=User.token_version))
        key = _key_of(s, settings, user_id)
        gens = s.scalar(select(func.count()).select_from(UsageEvent).where(
            _mine(user_id, key), UsageEvent.kind == "generate", UsageEvent.created_at >= start)) or 0
        if gens >= settings.daily_generations:
            raise ApiError(429, f"You've made today's {settings.daily_generations} free explainers. {reset_note()}", "quota_exceeded")
        running = s.scalar(select(func.count()).select_from(UsageEvent).where(
            UsageEvent.user_id == user_id, UsageEvent.kind == "generate", UsageEvent.finished.is_(False),
            UsageEvent.created_at >= now_ms() - STALE_MS)) or 0
        if running >= settings.max_concurrent:
            raise ApiError(429, f"You already have {running} explainers being made. Wait for one to finish.", "too_many_running")
        ev = UsageEvent(user_id=user_id, kind="generate", usd=0.0, finished=False, net=_net_of(s, user_id), email_key=key)
        s.add(ev)
        s.flush()
        return ev.id


def finish_generation(db: Database, event_id: str, usd: float, failed: bool = False) -> None:
    """Price the generation. A failed one stops counting as one of the day's explainers (its spend still counts)."""
    with db.session() as s:
        s.execute(update(UsageEvent).where(UsageEvent.id == event_id).values(
            usd=float(usd), finished=True, **({"kind": "generate-failed"} if failed else {})))


def record(settings: Settings, db: Database, user_id: str, kind: str, usd: float) -> None:
    """Log one paid call. If the account was deleted while the call ran, the spend is still logged, without a
    user (like the rows of a deleted account), instead of failing on the foreign key (audit F24)."""
    with db.session() as s:
        exists = s.scalar(select(User.id).where(User.id == user_id)) is not None
        s.add(UsageEvent(user_id=user_id if exists else None, kind=kind, usd=float(usd or 0.0), net=_net_of(s, user_id) if exists else None,
                         email_key=_key_of(s, settings, user_id) if exists else None))
