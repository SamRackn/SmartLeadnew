"""send attempt marker on drafts

A send job ran the Gmail call and the booking of its result in one
transaction. If anything failed after Gmail accepted the message -- the
commit, the worker process, the machine -- the transaction rolled back, the
queue saw an ordinary failure, and the retry sent the same email to the same
stranger again, up to five times.

``send_attempted_at`` is committed BEFORE the message is handed to the sender
and cleared only when the sender says unambiguously that nothing left. Set,
with no ``sent_messages`` row for the draft, means "may have been sent"; the
send step then holds the lead for a human to check the mailbox named by
``send_attempt_mailbox_id`` instead of trying again.

Both NULLABLE with no backfill: no existing draft has ever been handed to the
sender under this rule, and inventing an attempt would block a real send.

Revision ID: b3e1c9d4f2a7
Revises: a67a2d5777dc
Create Date: 2026-09-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e1c9d4f2a7'
down_revision: Union[str, Sequence[str], None] = 'a67a2d5777dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'drafts',
        sa.Column('send_attempted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'drafts',
        sa.Column('send_attempt_mailbox_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'drafts_send_attempt_mailbox_id_fkey',
        'drafts',
        'mailboxes',
        ['send_attempt_mailbox_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'drafts_send_attempt_mailbox_id_fkey', 'drafts', type_='foreignkey'
    )
    op.drop_column('drafts', 'send_attempt_mailbox_id')
    op.drop_column('drafts', 'send_attempted_at')
