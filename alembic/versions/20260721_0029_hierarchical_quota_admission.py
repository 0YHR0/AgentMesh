"""hierarchical quota admission

Revision ID: 20260721_0029
Revises: 20260721_0028
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_0029"
down_revision = "20260721_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("project_id", sa.String(length=128), server_default="default", nullable=False),
    )
    op.create_index(
        "ix_tasks_tenant_project_created_at", "tasks", ["tenant_id", "project_id", "created_at"]
    )
    op.create_table(
        "quota_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("max_concurrent_attempts", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('TENANT', 'PROJECT')", name="ck_quota_policies_scope"),
        sa.CheckConstraint(
            "(scope = 'TENANT' AND project_id IS NULL) OR "
            "(scope = 'PROJECT' AND project_id IS NOT NULL)",
            name="ck_quota_policies_scope_project",
        ),
        sa.CheckConstraint(
            "max_concurrent_attempts BETWEEN 1 AND 100000",
            name="ck_quota_policies_concurrency",
        ),
        sa.CheckConstraint("weight BETWEEN 1 AND 1000", name="ck_quota_policies_weight"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "scope", "project_id", "version", name="uq_quota_policy_version"
        ),
    )
    op.create_index(
        "uq_quota_policy_active_tenant", "quota_policies", ["tenant_id"], unique=True,
        postgresql_where=sa.text("active AND scope = 'TENANT'"),
    )
    op.create_index(
        "uq_quota_policy_active_project", "quota_policies", ["tenant_id", "project_id"],
        unique=True, postgresql_where=sa.text("active AND scope = 'PROJECT'"),
    )
    op.create_table(
        "quota_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_id"], ["quota_policies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "attempt_id", name="uq_quota_reservation_policy_attempt"),
    )
    op.create_index(
        "ix_quota_reservations_policy_active", "quota_reservations", ["policy_id", "released_at"]
    )
    op.create_index("ix_quota_reservations_attempt", "quota_reservations", ["attempt_id"])


def downgrade() -> None:
    op.drop_table("quota_reservations")
    op.drop_table("quota_policies")
    op.drop_index("ix_tasks_tenant_project_created_at", table_name="tasks")
    op.drop_column("tasks", "project_id")
