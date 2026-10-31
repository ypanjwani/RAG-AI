"""usage_events.email_key: the daily allowance survives deleting and re-registering an email (audit F25)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('usage_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email_key', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_usage_email_time', ['email_key', 'created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('usage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_email_time')
        batch_op.drop_column('email_key')
