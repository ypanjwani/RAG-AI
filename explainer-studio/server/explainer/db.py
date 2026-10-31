"""The database: SQLAlchemy models and sessions. Postgres in production, SQLite in development and tests.

Tables:
  users           an account (email + bcrypt hash); token_version invalidates every access token at once
  refresh_tokens  one row per refresh cookie, stored as a SHA-256 hash; rotated on every use (family_id links a chain)
  explainers      the reader's history: one row per ExplainerRecord, the whole record as JSON (the browser's shape)
  generations     what the server knows about a running or finished generation, so a reload can pick it up
  usage_events    every AI call a user paid for (generations, chat, edit, route, section): quotas read it
  user_preferences a reader's saved preferences (Feedback), added to the planner for that reader's pages only
  feedback        each Feedback message and what it became (a page change request and/or a preference)
  paper_cache     a paper's section filter and summaries, keyed by a hash of its text, shared by every user

Schema changes go through Alembic (server/migrations); tests build the tables with create_all.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # normalized: trimmed, lower-case
    password_hash: Mapped[str] = mapped_column(String(100))
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[str] = mapped_column(String(20), default="free")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    last_login_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The network the account was made from (middleware.network_of); usage is capped per network (audit F04).
    signup_net: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(32), nullable=True)  # set when rotated: reusing it means theft
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class Explainer(Base):
    __tablename__ = "explainers"
    # Record ids are made in the browser, so they are only unique per user.
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    size: Mapped[int] = mapped_column(Integer, default=0)  # bytes of data, for the per-user storage cap
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)  # the record's own updatedAt (browser clock)
    saved_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)  # server clock, last write


class Generation(Base):
    __tablename__ = "generations"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))  # running | done | error
    steps: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_user_time", "user_id", "created_at"), Index("ix_usage_time", "created_at"),
                      Index("ix_usage_net_time", "net", "created_at"), Index("ix_usage_email_time", "email_key", "created_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # SET NULL, not CASCADE: a deleted account's spend must stay in the ledger, or deleting and
    # re-registering would erase it from the global daily cap (audit F01).
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # generate | generate-failed | chat | edit | route | section | formula | feedback
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    finished: Mapped[bool] = mapped_column(Boolean, default=True)  # a generation is unfinished while it runs
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    # The user's signup_net, copied in so the per-network cap survives account deletion (audit F01/F04).
    net: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A keyed hash of the user's email (quota.email_key), so deleting the account and registering the same
    # address again doesn't reset the daily allowance (audit F25). Not reversible without the server secret.
    email_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UserPreference(Base):
    """A short, general preference the reader saved through Feedback (feedback.py). Only this user's pages use it:
    active ones are added to the planner's hint. Deleted with the account."""
    __tablename__ = "user_preferences"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str] = mapped_column(String(20))  # visuals | controls | level | length | tone | other
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | off | replaced
    feedback_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Feedback(Base):
    """One Feedback message about a page and what it became: a plan of changes for that page and/or a preference.
    Its cost is in usage_events (kind "feedback"). Deleted with the account."""
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(String(1000))
    page_request: Mapped[str] = mapped_column(String(1000), default="")
    preference_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    # Feedback v2: what the app understood (changes, keep, pageWide, question, the page's stage ids) and what the
    # reader confirmed before running it ({targets, keep, pageWide}); None until confirmed.
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PaperCache(Base):
    __tablename__ = "paper_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    used_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Database:
    def __init__(self, url: str):
        kw: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False}  # generations write from worker threads
            if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
                kw["poolclass"] = StaticPool  # one shared in-memory database (tests)
        self.engine: Engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _fk(conn, _record):  # SQLite enforces foreign keys (account deletion cascades) only when asked
                conn.execute("PRAGMA foreign_keys=ON")
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One unit of work: commits on success, rolls back on any error."""
        s = self._session()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()
