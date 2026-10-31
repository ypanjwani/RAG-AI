"""per-network caps: users.signup_net and usage_events.net (audit F04)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('signup_net', sa.String(length=64), nullable=True))
    with op.batch_alter_table('usage_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('net', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_usage_net_time', ['net', 'created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('usage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_net_time')
        batch_op.drop_column('net')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('signup_net')
