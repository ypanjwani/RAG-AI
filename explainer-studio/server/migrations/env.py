"""Alembic: migrates the database named by DATABASE_URL (explainer-studio/.env is read in development)."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from explainer.config import database_url
from explainer.db import Base

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
config = context.config
if config.config_file_name is not None:
    # keep loggers made before the migration (by default fileConfig turns them off, e.g. the app's in the test run)
    fileConfig(config.config_file_name, disable_existing_loggers=False)

URL = database_url(os.environ.get("DATABASE_URL"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"},
                      render_as_batch=URL.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        # SQLite can't ALTER most things in place; batch mode copies the table instead.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=URL.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
