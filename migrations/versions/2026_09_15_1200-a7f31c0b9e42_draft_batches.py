"""draft_batches: Batch API submissions and their measured cost

Revision ID: a7f31c0b9e42
Revises: c2dd03b8b825
Create Date: 2026-09-15 12:00:00.000000

Adds the one table bulk drafting needs. No change to any existing table: the
batch path writes ordinary `drafts` rows through the same `persist_draft` the
synchronous path uses, so nothing downstream -- verification, the review app,
delete-by-person -- can tell which path produced a draft, and nothing needs to.

Deliberately NOT cascade-deleted with a lead. A `draft_batches` row is a
billing and cost record covering hundreds of leads; it holds no personal data
(job ids and token counts only), and deleting a person must not blow a hole in
the cost history of an unrelated batch. Delete-by-person is unaffected: the
`jobs` rows it references are already cascade-deleted, and `_jobs_of` simply
finds fewer of them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a7f31c0b9e42"
down_revision: Union[str, Sequence[str], None] = "c2dd03b8b825"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "draft_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="in_progress"
        ),
        sa.Column("job_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage", JSONB(), nullable=False, server_default="{}"),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_batches")),
        sa.UniqueConstraint("batch_id", name=op.f("uq_draft_batches_batch_id")),
    )
    # The poller's query is "every batch that has not ended yet".
    op.create_index(
        op.f("ix_draft_batches_status"), "draft_batches", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_draft_batches_status"), table_name="draft_batches")
    op.drop_table("draft_batches")
