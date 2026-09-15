"""Add the reader-only coordinated Runtime drain projection.

This revision is schema-only.  No drain rows are created and no application
writer is activated by the migration.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260909_0052"
down_revision = "20260909_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coordination_runtime_drains",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "triggering_run_id",
            sa.Uuid(),
            sa.ForeignKey("task_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=4096), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAINING', 'COMPLETE')",
            name="ck_coordination_runtime_drains_status",
        ),
        sa.CheckConstraint(
            "target IN ('RUNNING', 'WAITING_APPROVAL', 'FAILED', 'CANCELED')",
            name="ck_coordination_runtime_drains_target",
        ),
        sa.CheckConstraint(
            "tenant_id = btrim(tenant_id) AND char_length(tenant_id) BETWEEN 1 AND 128",
            name="ck_coordination_runtime_drains_tenant",
        ),
        sa.CheckConstraint(
            "reason = btrim(reason) AND char_length(reason) BETWEEN 1 AND 4096",
            name="ck_coordination_runtime_drains_reason",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_coordination_runtime_drains_version",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_coordination_runtime_drains_timestamps",
        ),
        sa.CheckConstraint(
            "(status = 'DRAINING' AND completed_at IS NULL) OR "
            "(status = 'COMPLETE' AND completed_at IS NOT NULL AND "
            "completed_at >= created_at AND updated_at >= completed_at)",
            name="ck_coordination_runtime_drains_completion",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_coordination_runtime_drains_active_task",
        "coordination_runtime_drains",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'DRAINING'"),
    )
    op.create_index(
        "ix_coordination_runtime_drains_tenant_status_updated",
        "coordination_runtime_drains",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_coordination_runtime_drains_task_created",
        "coordination_runtime_drains",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT 1 FROM coordination_runtime_drains LIMIT 1")
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot downgrade 0052: coordination runtime drains contain rows; "
            "schema and data are unchanged"
        )
    op.drop_index(
        "ix_coordination_runtime_drains_task_created",
        table_name="coordination_runtime_drains",
    )
    op.drop_index(
        "ix_coordination_runtime_drains_tenant_status_updated",
        table_name="coordination_runtime_drains",
    )
    op.drop_index(
        "uq_coordination_runtime_drains_active_task",
        table_name="coordination_runtime_drains",
    )
    op.drop_table("coordination_runtime_drains")
