"""feedback.plan and feedback.confirmed: Feedback v2 keeps what the app understood and what the reader confirmed

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('confirmed', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.drop_column('confirmed')
        batch_op.drop_column('plan')
