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
    # Refuse rather than delete: a legacy row is what stops Email 1 going to
    # someone Smartlead already emailed. Roll back the application, not this.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM sent_messages WHERE draft_id IS NULL) THEN "
        "RAISE EXCEPTION 'refusing to downgrade: sent_messages holds legacy (Smartlead) "
        "sends; deleting them would let Email 1 go to people already emailed'; "
        "END IF; END $$"
    )
    op.alter_column('sent_messages', 'draft_id', existing_type=sa.Integer(), nullable=False)
    op.drop_column('sent_messages', 'legacy')
