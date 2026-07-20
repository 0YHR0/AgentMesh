"""Add governed outbound A2A delegation correlations.

Revision ID: 20260720_0020
Revises: 20260720_0019
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260720_0020"
down_revision: str | None = "20260720_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_tasks_execution_mode", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_execution_mode",
        "tasks",
        "execution_mode IN ('DIRECT', 'REVIEWED', 'COORDINATED', 'FEDERATED')",
    )
    op.create_table(
        "a2a_remote_task_correlations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("peer_id", sa.Uuid(), nullable=False),
        sa.Column("card_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("card_digest", sa.String(length=80), nullable=False),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=False),
        sa.Column("protocol_binding", sa.String(length=32), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("endpoint_tenant", sa.String(length=255), nullable=True),
        sa.Column("outbound_message_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("remote_task_id", sa.String(length=512), nullable=True),
        sa.Column("remote_context_id", sa.String(length=512), nullable=True),
        sa.Column("last_remote_state", sa.String(length=128), nullable=True),
        sa.Column("last_response_digest", sa.String(length=80), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("poll_count", sa.Integer(), nullable=False),
        sa.Column("late_result", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'SENDING', 'WAITING_REMOTE', 'OUTCOME_UNKNOWN', "
            "'INTERVENTION_REQUIRED', 'COMPLETED', 'FAILED', 'REJECTED', 'CANCELED')",
            name="ck_a2a_correlations_status",
        ),
        sa.CheckConstraint("poll_count >= 0", name="ck_a2a_correlations_poll_count"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["peer_id"], ["a2a_peers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["card_snapshot_id"], ["a2a_agent_card_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_a2a_correlations_task"),
        sa.UniqueConstraint("run_id", name="uq_a2a_correlations_run"),
        sa.UniqueConstraint("outbound_message_id", name="uq_a2a_correlations_message"),
    )
    op.create_index(
        "ix_a2a_correlations_tenant_status",
        "a2a_remote_task_correlations",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "uq_a2a_correlations_peer_remote_task",
        "a2a_remote_task_correlations",
        ["peer_id", "remote_task_id"],
        unique=True,
        postgresql_where=sa.text("remote_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tasks WHERE execution_mode = 'FEDERATED') "
            "THEN RAISE EXCEPTION 'cannot downgrade while FEDERATED Tasks exist'; END IF; END $$"
        )
    )
    op.drop_index(
        "uq_a2a_correlations_peer_remote_task",
        table_name="a2a_remote_task_correlations",
    )
    op.drop_index(
        "ix_a2a_correlations_tenant_status",
        table_name="a2a_remote_task_correlations",
    )
    op.drop_table("a2a_remote_task_correlations")
    op.drop_constraint("ck_tasks_execution_mode", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_execution_mode",
        "tasks",
        "execution_mode IN ('DIRECT', 'REVIEWED', 'COORDINATED')",
    )
