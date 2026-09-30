"""compliance plumbing: per-workspace postal address, scoped suppression, tombstones

* workspaces.postal_address (+ confirmed_by / confirmed_at). No backfill: an
  address nobody has confirmed must not render a footer, and the env value
  SENDER_POSTAL_ADDRESS was never confirmed by anyone.
* suppression_list.workspace_id (NULL = global), email_sha256, added_by.
  email_sha256 is backfilled for existing email rows so a tombstone lookup
  matches old entries too. The CHECK widens to admit a sha256-only tombstone.
* leads.approved_by widened to 320: it now holds a login email.

Revision ID: d5b2f3a4c6e7
Revises: c4a1e2f3b5d6
Create Date: 2026-09-30 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5b2f3a4c6e7'
down_revision: Union[str, Sequence[str], None] = 'c4a1e2f3b5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workspaces', sa.Column('postal_address', sa.Text(), nullable=True))
    op.add_column('workspaces', sa.Column('postal_address_confirmed_by', sa.String(length=320), nullable=True))
    op.add_column('workspaces', sa.Column('postal_address_confirmed_at', sa.DateTime(timezone=True), nullable=True))

    op.add_column('suppression_list', sa.Column('email_sha256', sa.String(length=64), nullable=True))
    op.add_column('suppression_list', sa.Column('workspace_id', sa.Integer(), nullable=True))
    op.add_column('suppression_list', sa.Column('added_by', sa.String(length=320), nullable=True))
    op.create_foreign_key(
        op.f('fk_suppression_list_workspace_id_workspaces'), 'suppression_list',
        'workspaces', ['workspace_id'], ['id'], ondelete='CASCADE',
    )
    op.create_index(op.f('ix_suppression_list_email_sha256'), 'suppression_list', ['email_sha256'])
    op.create_index(op.f('ix_suppression_list_workspace_id'), 'suppression_list', ['workspace_id'])
    op.execute(
        "UPDATE suppression_list SET email_sha256 = "
        "encode(sha256(convert_to(lower(btrim(email)), 'UTF8')), 'hex') "
        "WHERE email IS NOT NULL"
    )
    op.drop_constraint(op.f('ck_suppression_list_email_or_domain'), 'suppression_list', type_='check')
    op.create_check_constraint(
        'email_or_domain', 'suppression_list',
        'email IS NOT NULL OR domain IS NOT NULL OR email_sha256 IS NOT NULL',
    )

    op.alter_column('leads', 'approved_by', type_=sa.String(length=320), existing_nullable=True)


def downgrade() -> None:
    op.alter_column('leads', 'approved_by', type_=sa.String(length=64), existing_nullable=True)
    op.execute("DELETE FROM suppression_list WHERE email IS NULL AND domain IS NULL")
    op.drop_constraint(op.f('ck_suppression_list_email_or_domain'), 'suppression_list', type_='check')
    op.create_check_constraint('email_or_domain', 'suppression_list', 'email IS NOT NULL OR domain IS NOT NULL')
    op.drop_index(op.f('ix_suppression_list_workspace_id'), table_name='suppression_list')
    op.drop_index(op.f('ix_suppression_list_email_sha256'), table_name='suppression_list')
    op.drop_constraint(op.f('fk_suppression_list_workspace_id_workspaces'), 'suppression_list', type_='foreignkey')
    op.drop_column('suppression_list', 'added_by')
    op.drop_column('suppression_list', 'workspace_id')
    op.drop_column('suppression_list', 'email_sha256')
    op.drop_column('workspaces', 'postal_address_confirmed_at')
    op.drop_column('workspaces', 'postal_address_confirmed_by')
    op.drop_column('workspaces', 'postal_address')
