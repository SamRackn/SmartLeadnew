"""legacy sent_messages: Smartlead's sends, recorded so Email 1 is never repeated

Revision ID: c0a7e8f9b1d2
Revises: b9f6d7e8a0c1
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c0a7e8f9b1d2'
down_revision: Union[str, Sequence[str], None] = 'b9f6d7e8a0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sent_messages', sa.Column('legacy', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.alter_column('sent_messages', 'draft_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM sent_messages WHERE draft_id IS NULL")
    op.alter_column('sent_messages', 'draft_id', existing_type=sa.Integer(), nullable=False)
    op.drop_column('sent_messages', 'legacy')
