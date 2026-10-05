"""cost_entries: manually entered monthly costs per workspace

Revision ID: b9f6d7e8a0c1
Revises: a8e5c6d7f9b0
Create Date: 2026-10-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b9f6d7e8a0c1'
down_revision: Union[str, Sequence[str], None] = 'a8e5c6d7f9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cost_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('month', sa.Date(), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('entered_by', sa.String(length=320), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('amount >= 0', name=op.f('ck_cost_entries_amount_not_negative')),
        sa.CheckConstraint('extract(day from month) = 1', name=op.f('ck_cost_entries_month_is_first_day')),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_cost_entries_workspace_id_workspaces'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cost_entries')),
    )
    op.create_index(op.f('ix_cost_entries_workspace_id'), 'cost_entries', ['workspace_id'])
    op.create_index(op.f('ix_cost_entries_month'), 'cost_entries', ['month'])


def downgrade() -> None:
    op.drop_index(op.f('ix_cost_entries_month'), table_name='cost_entries')
    op.drop_index(op.f('ix_cost_entries_workspace_id'), table_name='cost_entries')
    op.drop_table('cost_entries')
