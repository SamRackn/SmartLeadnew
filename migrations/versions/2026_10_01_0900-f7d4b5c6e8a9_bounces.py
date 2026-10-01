"""bounces: sent_messages.bounced_at / bounce_code (feeds mailbox auto-pause)

Revision ID: f7d4b5c6e8a9
Revises: e6c3a4b5d7f8
Create Date: 2026-10-01 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7d4b5c6e8a9'
down_revision: Union[str, Sequence[str], None] = 'e6c3a4b5d7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sent_messages', sa.Column('bounced_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('sent_messages', sa.Column('bounce_code', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('sent_messages', 'bounce_code')
    op.drop_column('sent_messages', 'bounced_at')
