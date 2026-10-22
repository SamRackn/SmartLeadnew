"""session_epoch on users: logout and password change revoke old cookies

Revision ID: d1b8f9a0c2e3
Revises: c0a7e8f9b1d2
Create Date: 2026-10-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1b8f9a0c2e3'
down_revision: Union[str, Sequence[str], None] = 'c0a7e8f9b1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('session_epoch', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'session_epoch')
