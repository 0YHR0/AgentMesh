"""Add Virtual Company operating cycles and goals.

Revision ID: 20260729_0037
Revises: 20260729_0036
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0037"
down_revision = "20260729_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_operating_cycles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_schedule", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_operating_cycles_company_created",
        "company_operating_cycles",
        ["company_id", "created_at"],
    )
    op.create_index(
        "uq_company_operating_cycles_active",
        "company_operating_cycles",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "company_objectives",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("owner_position_id", sa.Uuid(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "priority >= 1 AND priority <= 5", name="ck_company_objective_priority"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["cycle_id"], ["company_operating_cycles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_position_id"], ["company_positions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_objectives_cycle_status",
        "company_objectives",
        ["cycle_id", "status"],
    )
    op.create_table(
        "company_key_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("objective_id", sa.Uuid(), nullable=False),
        sa.Column("metric_key", sa.String(128), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("baseline", sa.String(80), nullable=False),
        sa.Column("target", sa.String(80), nullable=False),
        sa.Column("current_verified_value", sa.String(80), nullable=True),
        sa.Column("current_estimated_value", sa.String(80), nullable=True),
        sa.Column("measurement_source", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["company_objectives.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "objective_id", "metric_key", name="uq_company_key_results_objective_metric"
        ),
    )
    op.create_index(
        "ix_company_key_results_objective_status",
        "company_key_results",
        ["objective_id", "status"],
    )
    op.create_table(
        "company_initiatives",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("objective_id", sa.Uuid(), nullable=False),
        sa.Column("owner_unit_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("outcome_contract", postgresql.JSONB(), nullable=False),
        sa.Column("budget_allocation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["company_objectives.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_unit_id"], ["organization_units.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_initiatives_objective_status",
        "company_initiatives",
        ["objective_id", "status"],
    )
    op.create_table(
        "company_initiative_tasks",
        sa.Column("initiative_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["initiative_id"], ["company_initiatives.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint(
            "initiative_id", "task_id", name="pk_company_initiative_tasks"
        ),
        sa.UniqueConstraint("task_id", name="uq_company_initiative_tasks_task"),
    )
    op.create_index(
        "ix_company_initiative_tasks_created",
        "company_initiative_tasks",
        ["initiative_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("company_initiative_tasks")
    op.drop_table("company_initiatives")
    op.drop_table("company_key_results")
    op.drop_table("company_objectives")
    op.drop_table("company_operating_cycles")
