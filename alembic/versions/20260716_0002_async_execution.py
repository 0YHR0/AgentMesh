"""Add durable asynchronous execution records.

Revision ID: 20260716_0002
Revises: 20260715_0001
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260716_0002"
down_revision: str | None = "20260715_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("tenant_id", sa.String(length=128), nullable=True))
    op.execute("UPDATE tasks SET tenant_id = 'default' WHERE tenant_id IS NULL")
    op.alter_column("tasks", "tenant_id", nullable=False)
    op.drop_index("ix_tasks_status_created_at", table_name="tasks")
    op.create_index(
        "ix_tasks_tenant_status_created_at",
        "tasks",
        ["tenant_id", "status", "created_at"],
    )

    op.add_column(
        "task_runs",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE task_runs SET queued_at = started_at WHERE queued_at IS NULL")
    op.alter_column("task_runs", "queued_at", nullable=False)
    op.alter_column("task_runs", "started_at", nullable=True)
    op.execute("UPDATE task_runs SET status = 'SUCCEEDED' WHERE status = 'COMPLETED'")
    op.drop_index("ix_task_runs_task_id_started_at", table_name="task_runs")
    op.create_index("ix_task_runs_task_id_queued_at", "task_runs", ["task_id", "queued_at"])

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_token"),
        sa.UniqueConstraint("run_id", "fencing_token", name="uq_attempt_run_fencing"),
    )
    op.create_index("ix_task_attempts_run_started_at", "task_attempts", ["run_id", "started_at"])
    op.create_index(
        "ix_task_attempts_status_lease",
        "task_attempts",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_pending_available",
        "outbox_events",
        ["status", "available_at", "created_at"],
    )

    op.create_table(
        "inbox_messages",
        sa.Column("consumer_name", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("consumer_name", "message_id"),
    )
    op.create_index("ix_inbox_processed_at", "inbox_messages", ["processed_at"])

    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "key"),
    )
    op.create_index("ix_idempotency_expires_at", "idempotency_records", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_inbox_processed_at", table_name="inbox_messages")
    op.drop_table("inbox_messages")
    op.drop_index("ix_outbox_pending_available", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_task_attempts_status_lease", table_name="task_attempts")
    op.drop_index("ix_task_attempts_run_started_at", table_name="task_attempts")
    op.drop_table("task_attempts")

    op.drop_index("ix_task_runs_task_id_queued_at", table_name="task_runs")
    op.execute("UPDATE task_runs SET status = 'COMPLETED' WHERE status = 'SUCCEEDED'")
    op.execute("UPDATE task_runs SET started_at = queued_at WHERE started_at IS NULL")
    op.alter_column("task_runs", "started_at", nullable=False)
    op.drop_column("task_runs", "queued_at")
    op.create_index("ix_task_runs_task_id_started_at", "task_runs", ["task_id", "started_at"])

    op.drop_index("ix_tasks_tenant_status_created_at", table_name="tasks")
    op.drop_column("tasks", "tenant_id")
    op.create_index("ix_tasks_status_created_at", "tasks", ["status", "created_at"])
