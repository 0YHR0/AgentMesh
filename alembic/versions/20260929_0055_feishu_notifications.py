"""Add independent, durable outbound Feishu notification jobs."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_0055"
down_revision = "20260927_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feishu_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("event_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "task_version", "event_kind",
            name="uq_feishu_notifications_task_transition",
        ),
    )
    op.create_index(
        "ix_feishu_notifications_claim",
        "feishu_notifications",
        ["status", "available_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_feishu_notifications_claim", table_name="feishu_notifications")
    op.drop_table("feishu_notifications")
