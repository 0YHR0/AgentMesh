"""shared replay bookmarks

Revision ID: 20260723_0031
Revises: 20260721_0030
"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_0031"
down_revision = "20260721_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "replay_bookmarks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "event_id", name="uq_replay_bookmark_task_event"
        ),
    )
    op.create_index(
        "ix_replay_bookmarks_task_created",
        "replay_bookmarks",
        ["tenant_id", "task_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("replay_bookmarks")
