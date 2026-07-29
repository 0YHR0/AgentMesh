"""Add the feature-gated Virtual Company organization model.

Revision ID: 20260729_0036
Revises: 20260729_0035
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260729_0036"
down_revision = "20260729_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("mission", sa.Text(), nullable=False),
        sa.Column("owner_principal_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("risk_policy_id", sa.Uuid(), nullable=True),
        sa.Column("default_currency", sa.String(3), nullable=False),
        sa.Column("operating_timezone", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companies_tenant_created", "companies", ["tenant_id", "created_at"])
    op.create_index(
        "uq_companies_tenant_active",
        "companies",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "organization_units",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(63), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("parent_unit_id", sa.Uuid(), nullable=True),
        sa.Column("budget_policy_id", sa.Uuid(), nullable=True),
        sa.Column("memory_namespace", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_unit_id"], ["organization_units.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "key", name="uq_organization_units_company_key"),
    )
    op.create_index(
        "ix_organization_units_company_status",
        "organization_units",
        ["company_id", "status"],
    )
    op.create_table(
        "company_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("primary_unit_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("responsibility_contract", postgresql.JSONB(), nullable=False),
        sa.Column("required_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_tool_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("memory_policy_id", sa.Uuid(), nullable=True),
        sa.Column("approval_scope", postgresql.JSONB(), nullable=False),
        sa.Column("budget_scope", postgresql.JSONB(), nullable=False),
        sa.Column("reports_to_position_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["primary_unit_id"], ["organization_units.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reports_to_position_id"], ["company_positions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "key", name="uq_company_positions_company_key"),
    )
    op.create_index(
        "ix_company_positions_unit_status",
        "company_positions",
        ["primary_unit_id", "status"],
    )
    op.create_table(
        "company_appointments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("agent_definition_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("appointed_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_definition_id"], ["agent_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["position_id"], ["company_positions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_appointments_agent",
        "company_appointments",
        ["agent_definition_id", "status"],
    )
    op.create_index(
        "ix_company_appointments_company_status",
        "company_appointments",
        ["company_id", "status"],
    )
    op.create_index(
        "uq_company_appointments_position_active",
        "company_appointments",
        ["position_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "organization_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(63), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "relationship_type",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "status",
            name="uq_organization_relationship_identity_status",
        ),
    )
    op.create_index(
        "ix_organization_relationships_company_status",
        "organization_relationships",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_organization_relationships_source",
        "organization_relationships",
        ["source_type", "source_id"],
    )
    op.create_index(
        "ix_organization_relationships_target",
        "organization_relationships",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_table("organization_relationships")
    op.drop_table("company_appointments")
    op.drop_table("company_positions")
    op.drop_table("organization_units")
    op.drop_table("companies")
