"""users, workspace memberships, audit events

Replaces the single env login (REVIEW_USER) with per-person accounts, so every
approval, rejection, edit and release names the human who did it.

No user is created here: a migration must not invent a password. Seed with
`python -m mazz.users seed-launch` and set passwords with `set-password`.

Revision ID: c4a1e2f3b5d6
Revises: b3e1c9d4f2a7
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c4a1e2f3b5d6'
down_revision: Union[str, Sequence[str], None] = 'b3e1c9d4f2a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=True),
        sa.Column('all_workspaces', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
        sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    )
    op.create_table(
        'user_workspaces',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_workspaces_user_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_user_workspaces_workspace_id_workspaces'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'workspace_id', name=op.f('pk_user_workspaces')),
    )
    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('user_email', sa.String(length=320), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target_type', sa.String(length=64), nullable=False),
        sa.Column('target_id', sa.String(length=64), nullable=True),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_audit_events_user_id_users'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events')),
    )
    op.create_index(op.f('ix_audit_events_user_id'), 'audit_events', ['user_id'])
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'])
    op.create_index(op.f('ix_audit_events_created_at'), 'audit_events', ['created_at'])
    op.create_index('ix_audit_events_target', 'audit_events', ['target_type', 'target_id'])


def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('user_workspaces')
    op.drop_table('users')
