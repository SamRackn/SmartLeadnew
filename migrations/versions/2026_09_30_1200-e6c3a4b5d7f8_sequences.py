"""sequences: steps table, lead name/state/sequence columns, draft step

* sequence_steps (campaign, step, wait_days, subject, body_template, enabled).
* leads.first_name / last_name / state (ZoomInfo), skip_reason,
  sequence_step / sequence_state / next_step_due_at.
* drafts.step. campaigns.signature, campaigns.max_active_per_company.

Revision ID: e6c3a4b5d7f8
Revises: d5b2f3a4c6e7
Create Date: 2026-09-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6c3a4b5d7f8'
down_revision: Union[str, Sequence[str], None] = 'd5b2f3a4c6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sequence_steps',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('step', sa.Integer(), nullable=False),
        sa.Column('wait_days', sa.Integer(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('body_template', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.CheckConstraint('step >= 1 AND wait_days >= 0', name=op.f('ck_sequence_steps_positive')),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_sequence_steps_campaign_id_campaigns'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_sequence_steps')),
        sa.UniqueConstraint('campaign_id', 'step', name='uq_sequence_steps_campaign_step'),
    )
    op.add_column('campaigns', sa.Column('signature', sa.Text(), nullable=True))
    op.add_column('campaigns', sa.Column('max_active_per_company', sa.Integer(), server_default=sa.text('2'), nullable=False))
    op.add_column('leads', sa.Column('first_name', sa.String(length=128), nullable=True))
    op.add_column('leads', sa.Column('last_name', sa.String(length=128), nullable=True))
    op.add_column('leads', sa.Column('state', sa.String(length=64), nullable=True))
    op.add_column('leads', sa.Column('skip_reason', sa.String(length=64), nullable=True))
    op.add_column('leads', sa.Column('sequence_step', sa.Integer(), server_default=sa.text('1'), nullable=False))
    op.add_column('leads', sa.Column('sequence_state', sa.String(length=16), nullable=True))
    op.add_column('leads', sa.Column('next_step_due_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_leads_skip_reason'), 'leads', ['skip_reason'])
    op.create_index(op.f('ix_leads_sequence_state'), 'leads', ['sequence_state'])
    op.add_column('drafts', sa.Column('step', sa.Integer(), server_default=sa.text('1'), nullable=False))


def downgrade() -> None:
    op.drop_column('drafts', 'step')
    op.drop_index(op.f('ix_leads_sequence_state'), table_name='leads')
    op.drop_index(op.f('ix_leads_skip_reason'), table_name='leads')
    for col in ('next_step_due_at', 'sequence_state', 'sequence_step', 'skip_reason', 'state', 'last_name', 'first_name'):
        op.drop_column('leads', col)
    op.drop_column('campaigns', 'max_active_per_company')
    op.drop_column('campaigns', 'signature')
    op.drop_table('sequence_steps')
