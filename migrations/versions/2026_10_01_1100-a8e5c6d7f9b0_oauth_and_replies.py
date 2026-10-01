"""oauth + replies: per-mailbox OAuth columns, needs_reconnect, replies, reply_tags

Revision ID: a8e5c6d7f9b0
Revises: f7d4b5c6e8a9
Create Date: 2026-10-01 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8e5c6d7f9b0'
down_revision: Union[str, Sequence[str], None] = 'f7d4b5c6e8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE mailbox_status ADD VALUE IF NOT EXISTS 'needs_reconnect'")

    op.add_column('mailboxes', sa.Column('oauth_refresh_token_enc', sa.Text(), nullable=True))
    op.add_column('mailboxes', sa.Column('oauth_state', sa.String(length=64), nullable=True))
    op.add_column('mailboxes', sa.Column('oauth_state_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('mailboxes', sa.Column('oauth_connected_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('mailboxes', sa.Column('gmail_history_id', sa.String(length=32), nullable=True))
    op.add_column('mailboxes', sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_mailboxes_oauth_state'), 'mailboxes', ['oauth_state'])

    op.create_table(
        'replies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lead_id', sa.Integer(), nullable=False),
        sa.Column('mailbox_id', sa.Integer(), nullable=True),
        sa.Column('provider_message_id', sa.String(length=128), nullable=False),
        sa.Column('provider_thread_id', sa.String(length=128), nullable=True),
        sa.Column('from_address', sa.String(length=320), nullable=False),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('classification', sa.String(length=24), nullable=False),
        sa.Column('needs_confirmation', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('ooo_return_date', sa.Date(), nullable=True),
        sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('handled_by', sa.String(length=320), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_replies_lead_id_leads'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['mailbox_id'], ['mailboxes.id'], name=op.f('fk_replies_mailbox_id_mailboxes'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_replies')),
        sa.UniqueConstraint('provider_message_id', name=op.f('uq_replies_provider_message_id')),
    )
    op.create_index(op.f('ix_replies_lead_id'), 'replies', ['lead_id'])
    op.create_index(op.f('ix_replies_provider_thread_id'), 'replies', ['provider_thread_id'])

    op.create_table(
        'reply_tags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reply_id', sa.Integer(), nullable=False),
        sa.Column('tag', sa.String(length=32), nullable=False),
        sa.Column('tagged_by', sa.String(length=320), nullable=False),
        sa.Column('tagged_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['reply_id'], ['replies.id'], name=op.f('fk_reply_tags_reply_id_replies'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_reply_tags')),
        sa.UniqueConstraint('reply_id', 'tag', name='uq_reply_tags_reply_id_tag'),
    )


def downgrade() -> None:
    op.drop_table('reply_tags')
    op.drop_index(op.f('ix_replies_provider_thread_id'), table_name='replies')
    op.drop_index(op.f('ix_replies_lead_id'), table_name='replies')
    op.drop_table('replies')
    op.drop_index(op.f('ix_mailboxes_oauth_state'), table_name='mailboxes')
    for col in ('last_synced_at', 'gmail_history_id', 'oauth_connected_at', 'oauth_state_at',
                'oauth_state', 'oauth_refresh_token_enc'):
        op.drop_column('mailboxes', col)
    # Postgres cannot drop an enum value; 'needs_reconnect' stays in the type.
