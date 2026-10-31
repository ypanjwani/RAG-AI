"""usage_events keep their rows when an account is deleted (user_id nullable, ON DELETE SET NULL)

Audit F01: with ON DELETE CASCADE, deleting an account erased its spend from the global daily cap.

The table is rebuilt (create new, copy, drop old, rename) because the original foreign key has no
name, so it can't be dropped portably on both SQLite and Postgres.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLS = "id, user_id, kind, usd, finished, created_at"


def _rebuild(nullable: bool, ondelete: str) -> None:
    op.create_table('usage_events_new',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=nullable),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('usd', sa.Float(), nullable=False),
    sa.Column('finished', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete=ondelete),
    sa.PrimaryKeyConstraint('id')
    )
    where = "" if nullable else " WHERE user_id IS NOT NULL"
    op.execute(f"INSERT INTO usage_events_new ({COLS}) SELECT {COLS} FROM usage_events{where}")
    op.drop_index('ix_usage_user_time', table_name='usage_events')
    op.drop_index('ix_usage_time', table_name='usage_events')
    op.drop_table('usage_events')
    op.rename_table('usage_events_new', 'usage_events')
    op.create_index('ix_usage_time', 'usage_events', ['created_at'], unique=False)
    op.create_index('ix_usage_user_time', 'usage_events', ['user_id', 'created_at'], unique=False)


def upgrade() -> None:
    _rebuild(nullable=True, ondelete='SET NULL')


def downgrade() -> None:
    # Rows of deleted accounts (user_id NULL) can't fit the old NOT NULL column and are dropped.
    _rebuild(nullable=False, ondelete='CASCADE')
